//! Preserve receiver evidence and interpret its conclusion qualification.
//! Qualification is not a statistical improvement and legacy reports remain unqualified.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

pub type Extensions = BTreeMap<String, Value>;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QualificationState {
    #[default]
    LegacyUnqualified,
    Qualified,
    Ineligible,
}

pub fn qualification_state(evidence: &Extensions) -> QualificationState {
    match evidence.get("qualification") {
        None => QualificationState::LegacyUnqualified,
        Some(q)
            if q.get("stage").and_then(Value::as_str) == Some("conclusion")
                && q.get("eligible").and_then(Value::as_bool) == Some(true) =>
        {
            QualificationState::Qualified
        }
        Some(_) => QualificationState::Ineligible,
    }
}

/// Compatibility does not turn a legacy result into a qualified game optimization.
/// Explicit unknown/blocked/malformed qualifications always prevent promotion.
pub fn permits_promotion(evidence: &Extensions) -> bool {
    qualification_state(evidence) != QualificationState::Ineligible
}
