# How agents are built

A practical guide to the parts every AI agent has, using Ops Autopilot as the worked example. The same structure applies whether you write the loop yourself, use a framework such as Google ADK or LangGraph, or build in n8n.

## 1. First decide: call, workflow or agent?

Most "agents" in job posts are really one of three things. Pick the simplest one that works.

| Level | What it is | Use it when | In Ops Autopilot |
|---|---|---|---|
| Single call | One prompt in, one answer out | The task is one step: classify, extract, summarize | `extract.py` reads a document into a schema |
| Workflow | Code runs fixed steps in a fixed order; the model fills in some steps | You know the steps in advance | The whole pipeline: extract → validate → decide → act |
| Agent | The model chooses which tools to call, in what order, until the goal is met | The steps depend on what it finds along the way | `agent.py` triage: which lookups to run depends on the issue |

Ask four questions before choosing an agent: Is the task hard to specify step by step? Is the result worth the extra cost and time? Can the model actually do it? Can mistakes be caught before they cause harm? If any answer is no, stay with a workflow.

## 2. The seven parts of an agent

```
             ┌────────────── 7. Evals & observability ───────────────┐
             │                                                       │
  1. Goal ──►│  2. Model ◄──► 4. Loop ◄──► 3. Tools ──► the world     │
             │                  │                                    │
             │            5. State / memory                          │
             │                  │                                    │
             │            6. Guardrails  ──►  human                  │
             └───────────────────────────────────────────────────────┘
```

### 1. Goal and instructions
The system prompt says what the agent is for, how to work and how to finish. Good instructions describe judgement ("treat it as the same supplier only if it is clearly the same business"), not keystrokes. See `SYSTEM` in [agent.py](../autopilot/agent.py).

The task input should carry all the context the agent needs up front. Here it is the extracted document plus the list of open issues.

### 2. Model
The reasoning engine. Choose the model and effort per step: extraction runs at `medium` effort, the agent at `high`. Change models only with evals in hand (part 7).

### 3. Tools
Functions the model can ask you to run. Each one has a name, a description the model reads, and a JSON schema for its input. Rules that matter:

- **Few and focused.** Four tools here: `lookup_supplier`, `find_similar_documents`, `get_policy`, `submit_resolution`.
- **Descriptions are prompts.** Say what the tool returns and when to use it.
- **Strict schemas** (`strict: true`, `additionalProperties: false`), so inputs always match.
- **Read before write.** This agent's tools are all read-only. Anything with side effects (paying, sending, deleting) stays outside the agent or behind approval.
- **A "finish" tool.** `submit_resolution` gives the agent a structured way to end, so you never have to parse its final answer out of free text.
- **Errors go back to the model**, as `is_error: true` results, rather than crashing the run. The model can then correct itself.

### 4. The loop
The core of every agent, and what frameworks hide from you:

```python
messages = [task]
for step in range(MAX_STEPS):                       # guardrail: hard limit
    response = model(system, tools, messages)
    messages.append(assistant_turn(response))       # keep the full turn, append-only
    if response.stop_reason == "refusal":  return hand_to_human()
    calls = tool_calls_in(response)
    if not calls:                                   # model stopped without finishing
        nudge_once_then_hand_to_human()
    results = []
    for call in calls:
        if call.name == "submit_resolution":
            return validated(call.input)            # finish
        results.append(run_tool(call))              # errors become is_error results
    messages.append(user_turn(results))             # all results together, matched by id
return hand_to_human("step limit")
```

The real version is `triage()` in [agent.py](../autopilot/agent.py), about 60 lines.

### 5. State and memory
- **Short-term:** the `messages` list. It grows each turn; keep it append-only.
- **Long-term:** what outlives one run. Here that is the SQLite store (past documents, used by `find_similar_documents`) and the supplier master data. Larger agents add a memory store or a vector database.
- **Run state:** document status, attempt counters and the event trace in `store.py`.

### 6. Guardrails
What keeps an agent safe enough to run unattended. This project uses every common one:

| Guardrail | Here |
|---|---|
| Step limit | `AUTOPILOT_AGENT_MAX_STEPS` (6) |
| Least privilege | Read-only tools only |
| Propose, then verify | The agent's answer is applied only if code can verify it, then the rules run again |
| Hard rules the agent can't override | "block" issues never reach the agent |
| Confidence threshold | Recommendations below 85% are ignored |
| Human in the loop | Anything unresolved goes to the review queue |
| Safe fallback | Refusal, error, bad input or step limit all go to a person |
| Idempotent actions | A retry can never record something twice |
| Drafts for outbound messages | Replies are drafted, never sent automatically |

### 7. Evals and observability
You can't improve what you don't measure.
- **Evals:** a fixed set of inputs with known correct outcomes, scored automatically. Here: 13 labelled cases, scored on field accuracy, routing and issue recall ([run_evals.py](../evals/run_evals.py)). Run them on every prompt or model change; CI fails if routing drops below 90%.
- **Unit tests with a scripted model:** [test_agent.py](../tests/test_agent.py) fakes the model's turns to test the loop itself: tool results matched by id, bad input retried, step limit, refusal, cost tracking.
- **Traces:** every step and tool call is an event you can read on the dashboard.
- **Cost and latency:** tracked per document from the API's usage numbers.

## 3. Common agent patterns

| Pattern | Idea | Example |
|---|---|---|
| Prompt chaining | Output of one step feeds the next | extract → validate → decide |
| Routing | Classify, then send to the right handler | `decide.py` sends to auto / review / ignore |
| Tool-using agent | Model loops over tools until done | the triage agent |
| Orchestrator and workers | A lead agent splits work and hands parts to sub-agents | A future "month-end close" agent that gives each supplier to a worker |
| Evaluator and optimizer | One model drafts, another grades, repeat | Grading reply drafts before a person sees them |
| Human in the loop | Pause for approval at risky points | The review queue |

## 4. Where frameworks fit

The loop above is what every framework implements for you. Knowing it by hand makes any framework easy to pick up, and easier to debug.

| Option | What it gives you | When to reach for it |
|---|---|---|
| Hand-written loop (this repo) | Full control and visibility | Learning, strict guardrails, small agents |
| Claude SDK Tool Runner | The loop, built in, with your tools | Most custom-tool agents with less code |
| Claude Agent SDK | Claude Code's full harness: file, shell and web tools, subagents | Coding and file-heavy agents you host yourself |
| Claude Managed Agents | Anthropic runs the loop and a sandbox; scheduled runs | Hosted, long-running or scheduled agents |
| Google ADK | Agents, multi-agent flows, sessions, eval runner, dev UI, Cloud Run deploy | Google Cloud shops; multi-agent apps |
| LangGraph | Agents as graphs with explicit state and checkpoints | Complex branching flows |
| n8n / Make / Zapier | Visual workflows with an AI step | Integrations-heavy automations; ops teams that don't code |

A good next step for this repo: rebuild `agent.py` in Google ADK with the same four tools, run the same evals, and compare the two in a short write-up.

## 5. Build order that works

1. Write down the job and 10 to 20 real examples with the right answers. That is your first eval set.
2. Solve it with a single call or a workflow. Measure it.
3. Find the cases that fail because the steps depend on the situation. Only those get an agent.
4. Give the agent the fewest, read-only tools that cover those cases, plus a finish tool.
5. Add the guardrails: step limit, verification, fallback to a person.
6. Add tracing and cost tracking before you go live.
7. Re-run the evals on every change, and add every new failure you see to the eval set.
