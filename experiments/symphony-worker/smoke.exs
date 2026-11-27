defmodule FactorySymphonySmoke.Worker do
  def eligible?(issue), do: Agent.get(__MODULE__, &Map.has_key?(&1.admissions, issue.id))

  def run(issue, orchestrator, opts) do
    {owner, outcome} =
      Agent.get_and_update(__MODULE__, fn state ->
        {Map.fetch!(state.admissions, issue.id), %{state | admissions: Map.delete(state.admissions, issue.id)}}
      end)

    test = Agent.get(__MODULE__, & &1.test)
    send(test, {:worker_started, issue.id, self(), orchestrator, opts})

    receive do
      {:perform, id} when id == issue.id ->
        receipt = fixture(["run", id, outcome])
        if receipt["owner"] != owner, do: raise("Factory Store returned a different owner")
        send(test, {:worker_result, id, receipt})

        case outcome do
          "waiting_human" -> {:defer, :waiting_human}
          "waiting_upstream" -> {:defer, :waiting_upstream}
          "ready_to_resume" -> {:defer, :idle}
        end
    after
      3_000 -> raise "Fixture worker did not receive its release"
    end
  end

  def fixture(args) do
    %{python: python, script: script, db: db} = Agent.get(__MODULE__, & &1)
    {output, 0} = System.cmd(python, ["-B", script, db | args], stderr_to_stdout: true)
    Jason.decode!(output)
  end
end

defmodule FactorySymphonySmoke do
  alias FactorySymphonySmoke.Worker
  alias SymphonyElixir.{AgentRuntimeSupervisor, Orchestrator, Workflow}
  alias SymphonyElixir.Tracker.{Issue, Memory}

  @files ["lib/symphony_elixir/orchestrator.ex", "lib/symphony_elixir/agent_runtime_supervisor.ex"]

  def main do
    original = Path.expand(System.fetch_env!("FACTORY_SYMPHONY_ROOT"))
    ebin = Path.wildcard(Path.join(original, "_build/dev/lib/*/ebin"))
    ensure(ebin != [], "Expected the installed compiled Symphony checkout")
    Enum.each(ebin, &Code.prepend_path/1)
    {:ok, _} = Application.ensure_all_started(:yamerl)
    Logger.configure(level: :info)

    home = Path.expand(System.tmp_dir!())
    scratch = Path.join(home, "factory-symphony-smoke-#{System.pid()}-#{System.unique_integer([:positive])}")
    File.mkdir!(scratch)
    try do
      patch_sources(original, scratch)
      execute(scratch)
    after
      if Path.dirname(scratch) == home, do: File.rm_rf!(scratch)
    end
  end

  defp patch_sources(original, scratch) do
    Enum.each(@files, fn relative ->
      target = Path.join(scratch, relative)
      File.mkdir_p!(Path.dirname(target))
      File.cp!(Path.join(original, relative), target)
    end)
    patch = Path.join(__DIR__, "worker-injection.patch")
    {output, status} = System.cmd("git", ["apply", "--whitespace=error", patch], cd: scratch, stderr_to_stdout: true)
    ensure(status == 0, "Could not apply the narrow injection patch: #{output}")
    Enum.each(@files, &Code.compile_file(Path.join(scratch, &1)))
  end

  defp execute(scratch) do
    python = System.get_env("FACTORY_PYTHON") || System.find_executable("python")
    ensure(is_binary(python), "Set FACTORY_PYTHON to a local Python executable")
    test = self()
    {:ok, _} =
      Agent.start_link(fn ->
        %{python: python, script: Path.join(__DIR__, "fixture_worker.py"),
          db: Path.join(scratch, "factory.db"), admissions: %{}, test: test}
      end, name: Worker)
    %{"tasks" => [first, second]} = Worker.fixture(["setup"])
    workflow = Path.join(scratch, "WORKFLOW.md")
    File.write!(workflow, """
    ---
    tracker:
      kind: memory
      active_states: ["Todo"]
      terminal_states: ["Done"]
    polling:
      interval_ms: 100
    workspace:
      root: #{Jason.encode!(Path.join(scratch, "symphony-workspaces"))}
    agent:
      max_concurrent_agents: 1
    codex:
      command: "exit 78"
    ---
    Disposable worker injection smoke.
    """)
    Workflow.set_workflow_file_path(workflow)
    Application.put_env(:symphony_elixir, :memory_tracker_issues, [])
    {:ok, supervisor} = AgentRuntimeSupervisor.start_link(worker_module: Worker)

    try do
      orch = Process.whereis(Orchestrator)
      :erlang.trace_pattern({Memory, :fetch_issues_by_ids, 1}, true, [:local])
      :erlang.trace(orch, true, [:call, {:tracer, self()}])
      app_server = SymphonyElixir.Codex.AppServer
      {:module, ^app_server} = Code.ensure_loaded(app_server)
      :erlang.trace_pattern({app_server, :start_session, 2}, true, [:local])
      :erlang.trace(:new, true, [:call, {:tracer, self()}])
      mark(first, "sylan", "waiting_human")
      mark(second, "matt", "waiting_upstream")
      Application.put_env(:symphony_elixir, :memory_tracker_issues, [
        struct!(Issue, id: first, identifier: "SMOKE-1", title: "Human handoff", state: "Todo",
                priority: 1, dispatchable: true),
        struct!(Issue, id: second, identifier: "SMOKE-2", title: "Upstream handoff", state: "Todo",
                priority: 2, dispatchable: true),
      ])

      first_pid = started_and_monitored(first, orch)
      assert_refresh(first, orch)
      assert_idle_for(250)
      first_receipt = release(first, first_pid, {:shutdown, {:worker_deferred, :waiting_human}})
      ensure(first_receipt["state"] == "waiting_human" and !first_receipt["runnable"], "First task did not park")
      ensure(is_binary(first_receipt["decision"]), "Factory Store did not persist a human decision")

      second_pid = started_and_monitored(second, orch)
      assert_refresh(second, orch)
      second_receipt = release(second, second_pid, {:shutdown, {:worker_deferred, :waiting_upstream}})
      ensure(second_receipt["state"] == "waiting_upstream" and !second_receipt["runnable"], "Second task did not park")
      assert_idle_for(350)
      state = :sys.get_state(orch)
      ensure(state.running == %{} and state.retry_attempts == %{} and MapSet.size(state.claimed) == 0,
             "Waiting work consumed a native execution slot or scheduled another attempt")

      answer = Worker.fixture(["decide", first_receipt["decision"]])
      ensure(answer == %{"task" => first, "owner" => "sylan", "runnable" => true}, "Decision did not release original owner")
      mark(first, "sylan", "ready_to_resume")
      resumed_pid = started_and_monitored(first, orch)
      ensure(resumed_pid != first_pid, "Continuation did not get a new native task slot")
      assert_refresh(first, orch)
      resumed = release(first, resumed_pid, {:shutdown, {:worker_deferred, :idle}})
      ensure(resumed["owner"] == first_receipt["owner"] and resumed["run"] != first_receipt["run"],
             "Continuation changed owner or reused a factory run")
      assert_idle_for(350)
      state = :sys.get_state(orch)
      ensure(state.running == %{} and state.retry_attempts == %{}, "Continuation kept an execution slot")
      receive do
        {:trace, _, :call, {^app_server, :start_session, _}} -> raise "Codex AppServer started during this smoke"
      after
        0 -> :ok
      end
      IO.puts(Jason.encode!(%{
        native_orchestrator: true, tracker: "memory", native_by_id_refreshes: 3, max_active_tasks_observed: 1,
        monitored_handoffs: [first_receipt["state"], second_receipt["state"], resumed["state"]],
        resumed_original_owner: resumed["owner"], model_invocations: 0
      }))
    after
      Supervisor.stop(supervisor)
    end
  end

  defp mark(id, owner, mode) do
    Agent.update(Worker, fn state -> %{state | admissions: Map.put(state.admissions, id, {owner, mode})} end)
  end

  defp started_and_monitored(id, orch) do
    receive do
      {:worker_started, ^id, pid, ^orch, opts} ->
        ensure(opts[:worker_host] == nil, "Smoke unexpectedly dispatched on SSH")
        state = :sys.get_state(orch)
        ensure(Map.keys(state.running) == [id] and state.running[id].pid == pid,
               "Native scheduler did not record only the admitted task")
        ensure(is_reference(state.running[id].ref), "Native scheduler did not monitor worker")
        ensure(Task.Supervisor.children(SymphonyElixir.TaskSupervisor) == [pid],
               "Native Task.Supervisor does not own the worker")
        pid
    after
      4_000 -> raise "Native orchestrator did not dispatch #{id}"
    end
  end

  defp assert_refresh(id, orch) do
    receive do
      {:trace, ^orch, :call, {Memory, :fetch_issues_by_ids, [[^id]]}} -> :ok
    after
      1_000 -> raise "Missing native Tracker by-ID revalidation for #{id}"
    end
  end

  defp release(id, pid, disposition) do
    ref = Process.monitor(pid)
    send(pid, {:perform, id})
    receipt =
      receive do
        {:worker_result, ^id, receipt} -> receipt
      after
        4_000 -> raise "Missing disposable factory receipt"
      end
    receive do
      {:DOWN, ^ref, :process, ^pid, ^disposition} -> receipt
    after
      1_000 -> raise "Missing monitored worker disposition"
    end
  end

  defp assert_idle_for(ms) do
    receive do
      {:worker_started, id, _, _, _} -> raise "Unexpected second worker for #{id}"
    after
      ms -> :ok
    end
  end

  defp ensure(true, _message), do: :ok
  defp ensure(false, message), do: raise(message)
end

FactorySymphonySmoke.main()
