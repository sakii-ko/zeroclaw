use chrono::Utc;
use parking_lot::Mutex;
use serde::Serialize;
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;
use std::time::Instant;

#[derive(Debug, Clone, Serialize)]
pub struct ChannelIngressStatus {
    pub last_inbound_at: String,
    pub sender: String,
    pub preview: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ActiveMessageStatus {
    pub id: u64,
    pub channel: String,
    pub sender: String,
    pub preview: String,
    pub started_at: String,
    pub elapsed_seconds: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct ActiveToolCallStatus {
    pub id: String,
    pub turn_id: String,
    pub channel: String,
    pub provider: String,
    pub model: String,
    pub tool: String,
    pub arguments_preview: String,
    pub iteration: usize,
    pub started_at: String,
    pub elapsed_seconds: u64,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct RuntimeStatusSnapshot {
    pub channel_ingress: BTreeMap<String, ChannelIngressStatus>,
    pub active_messages: Vec<ActiveMessageStatus>,
    pub active_tool_calls: Vec<ActiveToolCallStatus>,
}

struct ActiveMessageEntry {
    id: u64,
    channel: String,
    sender: String,
    preview: String,
    started_at: String,
    started: Instant,
}

struct ActiveToolCallEntry {
    id: String,
    turn_id: String,
    channel: String,
    provider: String,
    model: String,
    tool: String,
    arguments_preview: String,
    iteration: usize,
    started_at: String,
    started: Instant,
}

#[derive(Default)]
struct RuntimeStatusRegistry {
    channel_ingress: BTreeMap<String, ChannelIngressStatus>,
    active_messages: BTreeMap<u64, ActiveMessageEntry>,
    active_tool_calls: BTreeMap<String, ActiveToolCallEntry>,
}

static REGISTRY: OnceLock<Mutex<RuntimeStatusRegistry>> = OnceLock::new();
static MESSAGE_SEQ: AtomicU64 = AtomicU64::new(1);

fn registry() -> &'static Mutex<RuntimeStatusRegistry> {
    REGISTRY.get_or_init(|| Mutex::new(RuntimeStatusRegistry::default()))
}

fn now_rfc3339() -> String {
    Utc::now().to_rfc3339()
}

fn normalize_preview(preview: &str, limit: usize) -> String {
    let mut collapsed = preview.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.chars().count() > limit {
        collapsed = collapsed.chars().take(limit.saturating_sub(1)).collect::<String>();
        collapsed.push('…');
    }
    collapsed
}

pub struct ActiveMessageGuard {
    id: u64,
}

impl Drop for ActiveMessageGuard {
    fn drop(&mut self) {
        finish_message(self.id);
    }
}

pub fn track_message(channel: &str, sender: &str, preview: &str) -> ActiveMessageGuard {
    record_channel_inbound(channel, sender, preview);

    let id = MESSAGE_SEQ.fetch_add(1, Ordering::Relaxed);
    let entry = ActiveMessageEntry {
        id,
        channel: channel.to_string(),
        sender: sender.to_string(),
        preview: normalize_preview(preview, 120),
        started_at: now_rfc3339(),
        started: Instant::now(),
    };

    registry().lock().active_messages.insert(id, entry);
    ActiveMessageGuard { id }
}

pub fn finish_message(id: u64) {
    registry().lock().active_messages.remove(&id);
}

pub fn record_channel_inbound(channel: &str, sender: &str, preview: &str) {
    registry().lock().channel_ingress.insert(
        channel.to_string(),
        ChannelIngressStatus {
            last_inbound_at: now_rfc3339(),
            sender: sender.to_string(),
            preview: normalize_preview(preview, 160),
        },
    );
}

pub fn start_tool_call(
    id: &str,
    turn_id: &str,
    channel: &str,
    provider: &str,
    model: &str,
    iteration: usize,
    tool: &str,
    arguments_preview: &str,
) {
    registry().lock().active_tool_calls.insert(
        id.to_string(),
        ActiveToolCallEntry {
            id: id.to_string(),
            turn_id: turn_id.to_string(),
            channel: channel.to_string(),
            provider: provider.to_string(),
            model: model.to_string(),
            tool: tool.to_string(),
            arguments_preview: normalize_preview(arguments_preview, 160),
            iteration,
            started_at: now_rfc3339(),
            started: Instant::now(),
        },
    );
}

pub fn finish_tool_call(id: &str) {
    registry().lock().active_tool_calls.remove(id);
}

pub fn snapshot() -> RuntimeStatusSnapshot {
    let guard = registry().lock();
    let mut active_messages = guard
        .active_messages
        .values()
        .map(|entry| ActiveMessageStatus {
            id: entry.id,
            channel: entry.channel.clone(),
            sender: entry.sender.clone(),
            preview: entry.preview.clone(),
            started_at: entry.started_at.clone(),
            elapsed_seconds: entry.started.elapsed().as_secs(),
        })
        .collect::<Vec<_>>();
    active_messages.sort_by_key(|entry| entry.id);

    let mut active_tool_calls = guard
        .active_tool_calls
        .values()
        .map(|entry| ActiveToolCallStatus {
            id: entry.id.clone(),
            turn_id: entry.turn_id.clone(),
            channel: entry.channel.clone(),
            provider: entry.provider.clone(),
            model: entry.model.clone(),
            tool: entry.tool.clone(),
            arguments_preview: entry.arguments_preview.clone(),
            iteration: entry.iteration,
            started_at: entry.started_at.clone(),
            elapsed_seconds: entry.started.elapsed().as_secs(),
        })
        .collect::<Vec<_>>();
    active_tool_calls.sort_by(|a, b| a.started_at.cmp(&b.started_at));

    RuntimeStatusSnapshot {
        channel_ingress: guard.channel_ingress.clone(),
        active_messages,
        active_tool_calls,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn track_message_updates_ingress_and_clears_on_drop() {
        let initial_count = snapshot().active_messages.len();
        {
            let _guard = track_message("qq", "user_a", "hello from qq");
            let snapshot = snapshot();
            assert_eq!(snapshot.active_messages.len(), initial_count + 1);
            let ingress = snapshot
                .channel_ingress
                .get("qq")
                .expect("qq ingress should be recorded");
            assert_eq!(ingress.sender, "user_a");
        }
        assert_eq!(snapshot().active_messages.len(), initial_count);
    }

    #[test]
    fn track_tool_call_adds_and_removes_entry() {
        start_tool_call(
            "turn-1:1:0",
            "turn-1",
            "qq",
            "gpt",
            "gpt-5.4",
            1,
            "shell",
            "echo ok",
        );
        let current = snapshot();
        assert_eq!(current.active_tool_calls.len(), 1);
        assert_eq!(current.active_tool_calls[0].tool, "shell");
        finish_tool_call("turn-1:1:0");
        assert!(snapshot().active_tool_calls.is_empty());
    }
}
