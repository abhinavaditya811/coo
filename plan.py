"""
plan.py — running an ordered plan of capability steps.

Extracted from kernel.py with the executor injected, so the same logic drives
both the single-process Mac setup and the edge (where each step is executed over
HTTP on another machine). Deliberately knows nothing about HTTP, history, or the
capability registry — that keeps the subtle parts (reference substitution, the
skip branch, the status rollup) unit-testable on their own.
"""

import re

MAX_STEPS = 4                                    # bounds a bad decomposition
STEP_REF_RE = re.compile(r"\{\{step(\d+)\}\}")   # {{step1}} -> step 1's result


class ExecutorUnavailable(Exception):
    """The executor itself is unreachable — as opposed to a step failing.

    These are different failures and must be handled differently: a failed step
    says nothing about the next one, but an unreachable executor means every
    remaining step would just time out too.
    """


def run(steps, execute_fn):
    """Run steps in order, substituting {{stepN}} with step N's result.

    Steps are independent unless they reference each other, so a failure does
    not stop the rest. Two exceptions:
      - a step whose referenced result is missing is SKIPPED, never run with a
        blank parameter (that is what stops a failed clipboard read from texting
        someone an empty message);
      - if the executor goes away, the remainder is skipped rather than retried.
    """
    outcomes, results, unavailable = [], {}, None

    for i, step in enumerate(steps, start=1):
        capability, params = step["capability"], dict(step.get("params") or {})

        if unavailable:
            outcomes.append({"step": i, "capability": capability, "params": params,
                             "status": "skipped", "error": unavailable})
            continue

        missing = sorted({int(n) for v in params.values() if isinstance(v, str)
                          for n in STEP_REF_RE.findall(v)} - set(results))
        if missing:
            outcomes.append({
                "step": i, "capability": capability, "params": params,
                "status": "skipped",
                "error": f"needed the result of step {missing[0]}, which failed",
            })
            continue

        for key, value in params.items():
            if isinstance(value, str):
                params[key] = STEP_REF_RE.sub(
                    lambda m: results[int(m.group(1))], value)

        try:
            result = execute_fn(capability, params)
            results[i] = result
            outcomes.append({"step": i, "capability": capability,
                             "params": params, "status": "ok", "result": result})
        except ExecutorUnavailable as e:
            unavailable = str(e) or "executor unreachable"
            outcomes.append({"step": i, "capability": capability, "params": params,
                             "status": "skipped", "error": unavailable})
        except Exception as e:
            outcomes.append({"step": i, "capability": capability, "params": params,
                             "status": "error", "error": str(e)})

    return outcomes


def summarize(outcomes):
    """Roll outcomes into one response payload."""
    summary = " ".join(
        o["result"] if o["status"] == "ok" else "Error: " + o["error"]
        for o in outcomes)
    failed = [o for o in outcomes if o["status"] != "ok"]
    payload = {
        "status": "ok" if not failed
                  else ("error" if len(failed) == len(outcomes) else "partial"),
        "steps": outcomes,
        "result": summary,
    }
    if len(outcomes) == 1:
        # Keep the single-step response shape stable for existing clients.
        o = outcomes[0]
        payload["capability"], payload["params"] = o["capability"], o["params"]
        if o["status"] == "ok":
            payload["result"] = o["result"]
        else:
            payload["error"] = o["error"]
    return payload
