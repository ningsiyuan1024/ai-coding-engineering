defmodule FactorySymphony.Bootstrap do
  @moduledoc false
  @compile {:no_warn_undefined, [SymphonyElixir.Config, SymphonyElixir.PathSafety, SymphonyElixir.Workflow]}
  require Logger
  alias FactorySymphony.{Admission, Eligibility, Worker}

  @sources [
    {"lib/symphony_elixir/orchestrator.ex", "ffa6fd2ca66f775b93ca561adb60a61a416f8fb604ff247128d3638169fdab22"},
    {"lib/symphony_elixir/agent_runtime_supervisor.ex", "600ac1bd04126f671e77992284ce0083acf9b0130a8a200c7f6751a665042c6a"}
  ]

  def load(installed) do
    paths = Path.wildcard(Path.join(installed, "_build/dev/lib/*/ebin"))
    if paths == [], do: raise(ArgumentError, "FACTORY_SYMPHONY_ROOT must point to the compiled Elixir checkout")
    Enum.each(paths, &Code.prepend_path/1)
    {:ok, _} = Application.ensure_all_started(:yamerl)
    {:ok, _} = Application.ensure_all_started(:req)
    compile_worker_seam(installed)
    Code.require_file("worker_adapter.ex", __DIR__)
  end

  def start_runtime(workflow, python, checkout, opts \\ []) do
    SymphonyElixir.Workflow.set_workflow_file_path(workflow)
    System.put_env("FACTORY_SYMPHONY_PID", System.pid())
    settings = SymphonyElixir.Config.settings!()
    if settings.worker.ssh_hosts != [], do: raise(ArgumentError, "Factory Symphony uses local child processes")

    Application.put_env(:symphony_elixir, :factory_worker, %{python: python, checkout: checkout})
    worker = Keyword.get(opts, :worker_module, Worker)
    children = [
      SymphonyElixir.WorkflowStore,
      Eligibility,
      {SymphonyElixir.AgentRuntimeSupervisor, worker_module: worker}
    ]
    children =
      if Keyword.get(opts, :admission?, true),
        do: children ++ [{Admission, interval: 60_000}],
        else: children

    Supervisor.start_link(children, strategy: :rest_for_one)
  end

  def main(args) when args in [[], ["--check"]] do
    if linear_key?() do
      run(args)
    else
      IO.puts(:stderr, "Factory Symphony requires an authorized OSS_LINEAR_API_KEY or LINEAR_API_KEY.")
      System.halt(77)
    end
  end

  def main(_args) do
    IO.puts(:stderr, "Usage: elixir workflows/symphony/worker_boot.exs [--check]")
    System.halt(64)
  end

  defp run(args) do
    checkout = Path.expand("../..", __DIR__)
    factory_root = absolute_env("FACTORY_ROOT")
    installed = absolute_env("FACTORY_SYMPHONY_ROOT")
    python = python_executable()

    Enum.each(
      %{"FACTORY_STATE" => "factory.db", "FACTORY_PROFILES" => "profiles", "FACTORY_PLAN" => "model-plan.json"},
      fn {variable, default} -> System.put_env(variable, absolute_env(variable, Path.join(factory_root, default))) end
    )

    System.put_env("FACTORY_SYMPHONY_WORKSPACE", Path.join(factory_root, "symphony-worker-workspaces"))
    load(installed)
    workflow = Path.join(__DIR__, "worker_workflow.md")
    SymphonyElixir.Workflow.set_workflow_file_path(workflow)
    verify_workspace(factory_root)
    :ok = SymphonyElixir.Config.validate!()

    if args == ["--check"] do
      IO.puts("Factory Symphony worker extension validated without starting the scheduler.")
    else
      Process.flag(:trap_exit, true)
      {:ok, pid} = start_runtime(workflow, python, checkout)
      Logger.info("Factory Symphony native scheduler started with its dedicated local worker")

      receive do
        {:EXIT, ^pid, :normal} -> :ok
        {:EXIT, ^pid, _reason} -> System.halt(1)
      end
    end
  rescue
    error in [ArgumentError, RuntimeError, File.Error, KeyError, MatchError] ->
      IO.puts(:stderr, "Factory Symphony could not start: #{Exception.message(error)}")
      System.halt(1)
  end

  defp absolute_env(variable, default \\ nil) do
    path = System.get_env(variable) || default
    if !is_binary(path) or Path.type(path) != :absolute,
      do: raise(ArgumentError, "#{variable} must be an absolute path")
    Path.expand(path)
  end

  defp python_executable do
    path =
      if System.get_env("FACTORY_PYTHON"),
        do: absolute_env("FACTORY_PYTHON"),
        else: System.find_executable("python")
    if !is_binary(path) or !File.regular?(path), do: raise(ArgumentError, "A local Python executable is required")
    path
  end

  defp linear_key? do
    Enum.any?(["OSS_LINEAR_API_KEY", "LINEAR_API_KEY"], fn key ->
      System.get_env(key, "") |> String.trim() != ""
    end)
  end

  defp verify_workspace(factory_root) do
    {:ok, root} = SymphonyElixir.PathSafety.canonicalize(factory_root)
    {:ok, workspace} = SymphonyElixir.PathSafety.canonicalize(SymphonyElixir.Config.local_workspace_root())
    suffix = Path.relative_to(workspace, root)

    if suffix != "symphony-worker-workspaces",
      do: raise(ArgumentError, "Symphony workspace must be its own directory under FACTORY_ROOT")

    protected = Enum.map(["FACTORY_STATE", "FACTORY_PROFILES", "FACTORY_PLAN"], &System.fetch_env!/1)
    Enum.each([Path.join(root, "worktrees") | protected], fn path ->
      {:ok, target} = SymphonyElixir.PathSafety.canonicalize(path)
      left = path_parts(workspace)
      right = path_parts(target)
      if Enum.take(right, length(left)) == left or Enum.take(left, length(right)) == right,
        do: raise(ArgumentError, "A factory path overlaps Symphony workspace cleanup")
    end)
  end

  defp path_parts(path) do
    if match?({:win32, _}, :os.type()), do: Path.split(String.downcase(path)), else: Path.split(path)
  end

  defp compile_worker_seam(installed) do
    tmp = Path.expand(System.tmp_dir!())
    scratch = Path.join(tmp, "factory-symphony-worker-#{System.pid()}-#{System.unique_integer([:positive])}")
    File.mkdir!(scratch)

    try do
      files = Enum.map(@sources, fn {name, digest} ->
        source = File.read!(Path.join(installed, name))
        actual = Base.encode16(:crypto.hash(:sha256, source), case: :lower)
        if actual != digest, do: raise(ArgumentError, "Installed Symphony source revision changed at #{name}")
        target = Path.join(scratch, name)
        File.mkdir_p!(Path.dirname(target))
        File.write!(target, source)
        target
      end)

      patch = Path.join(__DIR__, "worker-injection.patch")
      {_output, status} = System.cmd("git", ["apply", "--whitespace=error", patch], cd: scratch, stderr_to_stdout: true)
      if status != 0, do: raise(ArgumentError, "Could not apply the Symphony worker seam to the scratch copy")
      previous = Code.compiler_options()

      try do
        Code.compiler_options(ignore_module_conflict: true)
        Enum.each(files, &Code.compile_file/1)
      after
        Code.compiler_options(Map.to_list(previous))
      end
    after
      if Path.dirname(scratch) == tmp, do: File.rm_rf!(scratch)
    end
  end
end
