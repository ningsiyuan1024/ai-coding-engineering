defmodule FactorySymphony.WorkerPort do
  @moduledoc false
  require Logger

  @max_output 1_048_576
  @cancel_grace_ms 5_000

  def call(python, cwd, args, timeout_ms) do
    owner = self()
    ticket = make_ref()

    {guard, guard_ref} =
      spawn_monitor(fn ->
        owner_ref = Process.monitor(owner)
        result = run_port(python, cwd, args, owner_ref, ticket, timeout_ms)
        send(owner, {ticket, result})
      end)

    await(guard, guard_ref, ticket)
  end

  def cancel(owner) when is_pid(owner), do: send(owner, {__MODULE__, :cancel})

  defp await(guard, guard_ref, ticket) do
    receive do
      {^ticket, result} ->
        Process.demonitor(guard_ref, [:flush])
        result

      {:DOWN, ^guard_ref, :process, ^guard, _reason} ->
        {:error, :port_guard_failed}

      {__MODULE__, :cancel} ->
        send(guard, {:cancel, ticket})
        await(guard, guard_ref, ticket)
    end
  end

  defp run_port(python, cwd, args, owner_ref, ticket, timeout_ms) do
    Process.flag(:trap_exit, true)

    port =
      Port.open({:spawn_executable, to_charlist(python)}, [
        :binary,
        :exit_status,
        :use_stdio,
        :hide,
        {:cd, to_charlist(cwd)},
        {:args, Enum.map(args, &to_charlist/1)}
      ])

    try do
      read(port, owner_ref, ticket, deadline(timeout_ms), "", 0)
    after
      close(port)
    end
  rescue
    _error -> {:error, :port_start_failed}
  end

  defp read(port, owner_ref, ticket, expiry, output, size) do
    receive do
      {^port, {:data, chunk}} when size + byte_size(chunk) <= @max_output ->
        read(port, owner_ref, ticket, expiry, [output, chunk], size + byte_size(chunk))

      {^port, {:data, _chunk}} ->
        stop(port)
        {:error, :output_limit}

      {^port, {:exit_status, 0}} ->
        decode(output)

      {^port, {:exit_status, _status}} ->
        {:error, :worker_nonzero_exit}

      {:EXIT, ^port, _reason} ->
        {:error, :worker_port_closed}

      {:DOWN, ^owner_ref, :process, _owner, _reason} ->
        stop(port)
        {:error, :owner_down}

      {:cancel, ^ticket} ->
        stop(port)
        {:error, :cancelled}
    after
      remaining(expiry) ->
        stop(port)
        {:error, :worker_timeout}
    end
  end

  defp decode(output) do
    case Jason.decode(IO.iodata_to_binary(output)) do
      {:ok, value} when is_map(value) -> {:ok, value}
      _ -> {:error, :invalid_worker_receipt}
    end
  end

  defp stop(port) do
    try do
      Port.command(port, "cancel\n", [:nosuspend])
    rescue
      ArgumentError -> false
    end

    unless await_exit(port, deadline(@cancel_grace_ms)) do
      hard_stop(port)
      await_exit(port, deadline(1_000))
    end
  end

  defp await_exit(port, expiry) do
    wait = remaining(expiry)
    if wait == 0 do
      false
    else
      receive do
        {^port, {:exit_status, _status}} -> true
        {:EXIT, ^port, _reason} -> true
        {^port, {:data, _chunk}} -> await_exit(port, expiry)
      after
        wait -> false
      end
    end
  end

  defp hard_stop(port) do
    case Port.info(port, :os_pid) do
      {:os_pid, pid} when is_integer(pid) and pid > 0 ->
        {command, args} = kill_command(pid)
        {_output, status} = System.cmd(command, args, stderr_to_stdout: true)
        if status == 0 do
          Logger.info("Forced stop requested for factory worker pid=#{pid}")
        else
          if Port.info(port) != nil, do: Logger.warning("Factory worker hard stop was not confirmed pid=#{pid}")
        end

      nil ->
        :ok
    end
  rescue
    _error -> Logger.error("Factory worker hard stop could not be executed")
  end

  defp kill_command(pid) do
    target = Integer.to_string(pid)

    case :os.type() do
      {:win32, _} ->
        taskkill = Path.join([System.get_env("SystemRoot", "C:/Windows"), "System32", "taskkill.exe"])
        {taskkill, ["/PID", target, "/T", "/F"]}

      {:unix, _} ->
        {"/bin/kill", ["-KILL", target]}
    end
  end

  defp close(port) do
    if Port.info(port) != nil, do: Port.close(port)
  rescue
    ArgumentError -> :ok
  end

  defp deadline(ms), do: System.monotonic_time(:millisecond) + ms
  defp remaining(expiry), do: max(0, expiry - System.monotonic_time(:millisecond))
end

defmodule FactorySymphony.Worker do
  @moduledoc false
  require Logger
  alias FactorySymphony.{Eligibility, WorkerPort}

  @id ~r/\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\z/
  @dispositions %{
    "waiting_human" => :waiting_human,
    "waiting_upstream" => :waiting_upstream,
    "idle" => :idle,
    "failed" => :failed
  }

  def eligible?(%{id: issue_id}) do
    valid_id?(issue_id) and Eligibility.take(issue_id)
  end

  def probe(issue_id) do
    case invoke(["-m", "factory.symphony_worker", "eligible", issue_id], 8_000) do
      {:ok, %{"eligible" => value}} when is_boolean(value) ->
        value

      result ->
        Logger.warning("Factory eligibility unavailable for issue_id=#{issue_id} reason=#{fault(result)}")
        false
    end
  end

  def run(%{id: issue_id}, _recipient, opts) do
    cond do
      opts[:worker_host] != nil ->
        {:worker_fault, :ssh_worker_unsupported}

      not valid_id?(issue_id) ->
        {:worker_fault, :invalid_issue_identity}

      true ->
        invoke(["-m", "factory.symphony_worker", "execute", issue_id], 1_200_000)
        |> disposition()
    end
  end

  defp disposition({:ok, %{"disposition" => value}} = result) do
    case Map.fetch(@dispositions, value) do
      {:ok, status} -> {:defer, status}
      :error -> {:worker_fault, fault(result)}
    end
  end

  defp disposition(result), do: {:worker_fault, fault(result)}

  def invoke(args, timeout) do
    %{python: python, checkout: checkout} = Application.fetch_env!(:symphony_elixir, :factory_worker)
    WorkerPort.call(python, checkout, ["-B" | args], timeout)
  end

  defp valid_id?(value), do: is_binary(value) and Regex.match?(@id, value)
  defp fault({:error, reason}), do: reason
  defp fault(_result), do: :invalid_worker_receipt
end

defmodule FactorySymphony.Eligibility do
  @moduledoc false
  use GenServer
  alias FactorySymphony.{Worker, WorkerPort}

  @max_parallel 4
  @ttl_ms 45_000

  def start_link(_opts), do: GenServer.start_link(__MODULE__, :ok, name: __MODULE__)
  def take(issue_id), do: GenServer.call(__MODULE__, {:take, issue_id})

  @impl true
  def init(:ok) do
    Process.flag(:trap_exit, true)
    {:ok, %{cache: %{}, queue: :queue.new(), tasks: %{}}}
  end

  @impl true
  def handle_call({:take, id}, _from, state) do
    now = System.monotonic_time(:millisecond)

    case Map.get(state.cache, id) do
      {eligible, expires} when expires > now ->
        cache = if eligible, do: Map.put(state.cache, id, {false, now + 5_000}), else: state.cache
        {:reply, eligible, %{state | cache: cache}}

      :pending ->
        {:reply, false, state}

      _ ->
        state = %{state | queue: :queue.in(id, state.queue), cache: Map.put(state.cache, id, :pending)}
        {:reply, false, fill_slots(state)}
    end
  end

  @impl true
  def handle_info({ref, eligible}, state) when is_reference(ref) and is_boolean(eligible) do
    Process.demonitor(ref, [:flush])
    {:noreply, record(state, ref, eligible)}
  end

  def handle_info({:DOWN, ref, :process, _pid, _reason}, state),
    do: {:noreply, record(state, ref, false)}

  def handle_info({:EXIT, _pid, _reason}, state), do: {:noreply, state}

  @impl true
  def terminate(_reason, state) do
    Enum.each(state.tasks, fn {_ref, {_id, task}} -> WorkerPort.cancel(task.pid) end)
  end

  defp record(state, ref, eligible) do
    case Map.pop(state.tasks, ref) do
      {{id, _task}, tasks} ->
        now = System.monotonic_time(:millisecond)
        cache = Map.reject(state.cache, fn {_id, entry} -> match?({_value, expires} when expires <= now, entry) end)
        if eligible, do: spawn(fn -> SymphonyElixir.Orchestrator.request_refresh() end)
        fill_slots(%{state | cache: Map.put(cache, id, {eligible, now + @ttl_ms}), tasks: tasks})

      {nil, _tasks} ->
        state
    end
  end

  defp fill_slots(state) when map_size(state.tasks) >= @max_parallel, do: state
  defp fill_slots(state) do
    case :queue.out(state.queue) do
      {{:value, id}, queue} ->
        task = Task.async(fn -> Worker.probe(id) end)
        fill_slots(%{state | queue: queue, tasks: Map.put(state.tasks, task.ref, {id, task})})

      {:empty, _queue} ->
        state
    end
  end
end

defmodule FactorySymphony.Admission do
  @moduledoc false
  use GenServer
  require Logger
  alias FactorySymphony.{Worker, WorkerPort}

  def start_link(opts), do: GenServer.start_link(__MODULE__, opts)

  @impl true
  def init(opts) do
    Process.flag(:trap_exit, true)
    send(self(), :poll)
    {:ok, %{interval: Keyword.fetch!(opts, :interval), task: nil}}
  end

  @impl true
  def handle_info(:poll, %{task: nil} = state) do
    task = Task.async(fn -> Worker.invoke(["-m", "factory.admission_service"], 300_000) end)
    {:noreply, %{state | task: task}}
  end

  def handle_info({ref, result}, %{task: %Task{ref: ref}} = state) do
    Process.demonitor(ref, [:flush])

    case result do
      {:ok, _report} -> :ok
      {:error, reason} -> Logger.warning("Factory admission service unavailable reason=#{reason}")
    end

    continue(state)
  end

  def handle_info({:DOWN, ref, :process, _pid, _reason}, %{task: %Task{ref: ref}} = state) do
    Logger.warning("Factory admission task exited without a report")
    continue(state)
  end

  def handle_info({:EXIT, _pid, _reason}, state), do: {:noreply, state}

  @impl true
  def terminate(_reason, %{task: %Task{pid: pid}}), do: WorkerPort.cancel(pid)
  def terminate(_reason, _state), do: :ok

  defp continue(state) do
    Process.send_after(self(), :poll, state.interval)
    {:noreply, %{state | task: nil}}
  end
end
