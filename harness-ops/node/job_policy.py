"""Execution guarantees shared by admission and the independently running worker."""


def process_policies():
    # Advertise implemented backends, not guesses based on OS or filesystem paths.
    return {
        "process_group": {"available": True, "backend": "posix_process_group",
                          "completion_scope": "original_process_group",
                          "reason": "Only the original process group is managed; detached descendants are unverified."},
        "contained": {"available": False, "backend": None, "completion_scope": None,
                      "reason": "This node has no verified backend containing descendants across process-group changes."},
    }


class PolicyUnavailable(ValueError):
    def __init__(self, policy, capability):
        super().__init__(capability["reason"])
        self.payload = {"error": str(self), "code": "execution_policy_unavailable",
                        "execution_policy": policy, "capability": capability}


def requested_policy(spec):
    if not isinstance(spec, dict):
        raise ValueError("job spec must be an object")
    policy = spec.get("execution_policy", "process_group")
    if not isinstance(policy, str) or policy not in process_policies():
        raise ValueError("execution_policy must be process_group or contained")
    return policy


def require_policy(spec):
    policy = requested_policy(spec)
    capability = process_policies()[policy]
    if not capability["available"]:
        raise PolicyUnavailable(policy, capability)
    return policy


def request_facts(spec):
    return {"execution_policy": spec.get("execution_policy", "process_group"),
            "execution_policy_source": "explicit" if "execution_policy" in spec else "legacy_default"}

