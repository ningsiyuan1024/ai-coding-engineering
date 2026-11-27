# One native tracker reconciliation. No application supervisor, worker, or model.
root = System.fetch_env!("FACTORY_SYMPHONY_ROOT")
paths = Path.wildcard(Path.join(root, "_build/dev/lib/*/ebin"))
if paths == [], do: raise("Expected a compiled Symphony Elixir checkout")
Enum.each(paths, &Code.prepend_path/1)
Logger.configure(level: :emergency)
{:ok, _} = Application.ensure_all_started(:req)
SymphonyElixir.Workflow.set_workflow_file_path(Path.join(__DIR__, "WORKFLOW.md"))

defmodule FactoryIntake do
  def poll do
    with {:ok, settings} <- SymphonyElixir.Config.settings(),
         %{kind: "linear", active_states: [_ | _] = states} = tracker <- settings.tracker,
         {:ok, issues} <- SymphonyElixir.Tracker.fetch_issues_by_states(states) do
      ids = issues
        |> Enum.filter(&SymphonyElixir.Tracker.Issue.routable?(&1, tracker.required_labels))
        |> Enum.map(& &1.id)
        |> Enum.uniq()
      {:ok, %{project_slug: tracker.project_slug, states: states, ids: ids}}
    else
      {:error, reason} -> {:error, reason}
      _ -> {:error, :invalid_tracker}
    end
  end
end

case FactoryIntake.poll() do
  {:ok, snapshot} -> IO.puts(Jason.encode!(snapshot))
  {:error, reason} ->
    status = case reason do
      :missing_linear_api_token -> 77
      {:linear_api_status, code} when code in [401, 403] -> 77
      {:linear_api_status, 429} -> 75
      {:linear_graphql_errors, errors} when is_list(errors) ->
        codes = Enum.map(errors, &get_in(&1, ["extensions", "code"]))
        cond do
          "RATELIMITED" in codes -> 75
          Enum.any?(codes, &(&1 in ["UNAUTHENTICATED", "FORBIDDEN", "AUTHENTICATION_ERROR"])) -> 77
          true -> 1
        end
      _ -> 1
    end
    IO.puts(:stderr, "Native Symphony tracker read failed (exit #{status}).")
    System.halt(status)
end
