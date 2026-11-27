Code.require_file("worker_bootstrap.ex", __DIR__)

defmodule FactorySymphony.WorkerSmoke do
  alias FactorySymphony.{Bootstrap, WorkerPort}
  alias SymphonyElixir.{Orchestrator, Tracker.Issue}
  @compile {:no_warn_undefined, [FactorySymphony.WorkerPort, Jason]}

  @id "00000000-0000-4000-8000-000000000001"

  def main do
    installed = Path.expand(System.fetch_env!("FACTORY_SYMPHONY_ROOT"))
    python = System.get_env("FACTORY_PYTHON") || System.find_executable("python")
    Bootstrap.load(installed)
    base = Path.expand(System.tmp_dir!())
    root = Path.join(base, "factory-worker-test-#{System.pid()}-#{System.unique_integer([:positive])}")
    File.mkdir!(root)

    try do
      verify_native_dispatch(root, python)
      verify_real_python_receipts(root, python)
      verify_cancellation(root, python)
      IO.puts(Jason.encode!(%{
        production_bootstrap: "passed", native_worker_slot: "released", tracker: "memory",
        external_worker: "disposable Python fixture", credentials: 0,
        real_python_worker: ["eligible:false", "waiting_human"],
        cooperative_owner_cancellation: "passed",
        windows_forced_child_tree_cancellation: if(match?({:win32, _}, :os.type()), do: "passed", else: "not_applicable")
      }))
    after
      if Path.dirname(root) == base, do: remove_scratch(root, 5)
    end
  end

  defp remove_scratch(path, retries) do
    case File.rm_rf(path) do
      {:ok, _} -> :ok
      {:error, reason, _file} when retries == 0 -> IO.puts(:stderr, "Smoke scratch cleanup failed: #{reason}")
      {:error, _reason, _file} ->
        Process.sleep(250)
        remove_scratch(path, retries - 1)
    end
  end

  defp verify_native_dispatch(root, python) do
    package = Path.join(root, "factory")
    File.mkdir!(package)
    File.write!(Path.join(package, "__init__.py"), "")
    File.cp!(Path.join(__DIR__, "worker_fixture.py"), Path.join(package, "symphony_worker.py"))
    workflow = Path.join(root, "WORKFLOW.md")
    File.write!(workflow, """
    ---
    tracker:
      kind: memory
      active_states: ["Todo"]
      terminal_states: ["Done"]
    polling:
      interval_ms: 100
    workspace:
      root: #{Jason.encode!(Path.join(root, "native-workspaces"))}
    agent:
      max_concurrent_agents: 1
    codex:
      command: "exit 78"
    ---
    Credential-free production-worker fixture.
    """)
    issue = struct!(Issue, id: @id, identifier: "OTP-100", title: "Worker integration", state: "Todo",
                    dispatchable: true)
    Application.put_env(:symphony_elixir, :memory_tracker_issues, [issue])
    File.write!(Path.join(root, "allow-" <> @id), "")
    {:ok, supervisor} = Bootstrap.start_runtime(workflow, python, root, admission?: false)

    try do
      orch = Process.whereis(Orchestrator)
      await(fn -> File.exists?(Path.join(root, "started-" <> @id)) end, 12_000)
      %{running: running} = :sys.get_state(orch)
      %{pid: pid, ref: ref, workspace_path: nil} = Map.fetch!(running, @id)
      true = is_reference(ref)
      [^pid] = Task.Supervisor.children(SymphonyElixir.TaskSupervisor)
      monitor = Process.monitor(pid)
      File.write!(Path.join(root, "release-" <> @id), "")

      receive do
        {:DOWN, ^monitor, :process, ^pid, {:shutdown, {:worker_deferred, :waiting_human}}} -> :ok
      after
        5_000 -> raise "Production adapter did not deliver the monitored disposition"
      end

      await(fn ->
        state = :sys.get_state(orch)
        state.running == %{} and state.retry_attempts == %{} and state.blocked == %{}
      end, 5_000)
    after
      Supervisor.stop(supervisor)
    end
  end

  defp verify_cancellation(root, python) do
    script = Path.join(__DIR__, "worker_fixture.py")
    owner = spawn(fn -> WorkerPort.call(python, root, ["-B", script, "cooperative", @id], 30_000) end)
    await(fn -> File.exists?(Path.join(root, "cooperative-started")) end, 8_000)
    Process.exit(owner, :kill)
    target = Path.join(root, "cooperative-cancelled")
    await(fn -> File.read(target) == {:ok, "cancel"} end, 8_000)

    if match?({:win32, _}, :os.type()) do
      owner = spawn(fn -> WorkerPort.call(python, root, ["-B", script, "stubborn", @id], 30_000) end)
      heartbeat = Path.join(root, "heartbeat")
      await(fn -> File.exists?(heartbeat) end, 8_000)
      Process.exit(owner, :kill)
      Process.sleep(7_500)
      size = File.stat!(heartbeat).size
      Process.sleep(350)
      if File.stat!(heartbeat).size != size do
        pids = File.read!(Path.join(root, "stubborn-started"))
        raise "Cancelled Windows worker left its child process running: #{pids}"
      end
    end
  end

  defp verify_real_python_receipts(scratch, python) do
    root = Path.join(scratch, "real-python")
    profiles = Path.join(root, "profiles")
    state = Path.join(root, "factory.db")
    plan = Path.join(root, "model-plan.json")
    checkout = Path.expand("../..", __DIR__)
    File.mkdir_p!(profiles)
    File.write!(plan, "{}")
    script = """
    import sys
    from pathlib import Path
    from factory.state import Store
    database, task = sys.argv[1:]
    store = Store(database)
    store.discover(task, "OTP-100", "https://github.com/owner/repository/issues/1", "Disposable operation")
    store.claim(task, "sylan", "fixture-revision", Path(database).parent / "worktrees" / task, "fixture-base")
    store.park(task, "sylan", question="Pause for review?")
    """
    {_output, 0} = System.cmd(python, ["-B", "-c", script, state, @id], cd: checkout, stderr_to_stdout: true)
    System.put_env(%{"FACTORY_ROOT" => root, "FACTORY_PROFILES" => profiles,
                     "FACTORY_STATE" => state, "FACTORY_PLAN" => plan})
    {:ok, %{"eligible" => false}} =
      WorkerPort.call(python, checkout, ["-B", "-m", "factory.symphony_worker", "eligible", @id], 12_000)
    {:ok, %{"disposition" => "waiting_human"}} =
      WorkerPort.call(python, checkout, ["-B", "-m", "factory.symphony_worker", "execute", @id], 12_000)
  end

  defp await(condition, timeout) do
    try_until(condition, System.monotonic_time(:millisecond) + timeout)
  end

  defp try_until(condition, expiry) do
    cond do
      condition.() -> :ok
      System.monotonic_time(:millisecond) >= expiry -> raise "Timed out waiting for worker evidence"
      true ->
        Process.sleep(25)
        try_until(condition, expiry)
    end
  end
end

FactorySymphony.WorkerSmoke.main()
