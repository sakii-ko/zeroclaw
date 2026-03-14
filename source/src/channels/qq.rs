use super::traits::{Channel, ChannelMessage, SendMessage};
use async_trait::async_trait;
use base64::{engine::general_purpose::STANDARD, Engine as _};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::RwLock;
use tokio_tungstenite::tungstenite::Message;
use uuid::Uuid;

const QQ_API_BASE: &str = "https://api.sgroup.qq.com";
const QQ_AUTH_URL: &str = "https://bots.qq.com/app/getAppAccessToken";

fn ensure_https(url: &str) -> anyhow::Result<()> {
    if !url.starts_with("https://") {
        anyhow::bail!(
            "Refusing to transmit sensitive data over non-HTTPS URL: URL scheme must be https"
        );
    }
    Ok(())
}

fn is_image_filename(filename: &str) -> bool {
    let lower = filename.to_ascii_lowercase();
    lower.ends_with(".png")
        || lower.ends_with(".jpg")
        || lower.ends_with(".jpeg")
        || lower.ends_with(".gif")
        || lower.ends_with(".webp")
        || lower.ends_with(".bmp")
        || lower.ends_with(".heic")
        || lower.ends_with(".heif")
        || lower.ends_with(".svg")
}

fn is_video_filename(filename: &str) -> bool {
    let lower = filename.to_ascii_lowercase();
    lower.ends_with(".mp4")
        || lower.ends_with(".mov")
        || lower.ends_with(".mkv")
        || lower.ends_with(".avi")
        || lower.ends_with(".webm")
}

fn is_voice_filename(filename: &str) -> bool {
    let lower = filename.to_ascii_lowercase();
    lower.ends_with(".silk")
        || lower.ends_with(".ogg")
        || lower.ends_with(".oga")
        || lower.ends_with(".opus")
}

fn target_looks_like_gif(target: &str) -> bool {
    if is_http_url(target) {
        return filename_from_url(target)
            .map(|filename| filename.to_ascii_lowercase().ends_with(".gif"))
            .unwrap_or(false);
    }

    if is_data_url(target) {
        return target
            .split(';')
            .next()
            .map(|prefix| prefix.to_ascii_lowercase().contains("image/gif"))
            .unwrap_or(false);
    }

    expand_local_attachment_path(target)
        .file_name()
        .and_then(|name| name.to_str())
        .map(|name| name.to_ascii_lowercase().ends_with(".gif"))
        .unwrap_or(false)
}

fn normalize_rich_media_kind(kind: QQAttachmentKind, target: &str) -> QQAttachmentKind {
    if kind == QQAttachmentKind::Video && target_looks_like_gif(target) {
        QQAttachmentKind::Image
    } else {
        kind
    }
}

fn is_http_url(target: &str) -> bool {
    target.starts_with("https://") || target.starts_with("http://")
}

fn sanitize_filename(filename: &str) -> String {
    let mut cleaned: String = filename
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_') {
                c
            } else {
                '_'
            }
        })
        .collect();
    cleaned = cleaned
        .trim_matches(|c| c == '.' || c == '_' || c == ' ')
        .to_string();
    if cleaned.is_empty() {
        "attachment.bin".to_string()
    } else {
        cleaned
    }
}

fn filename_from_url(url: &str) -> Option<String> {
    let normalized = url
        .split('?')
        .next()
        .unwrap_or(url)
        .split('#')
        .next()
        .unwrap_or(url);
    Path::new(normalized)
        .file_name()
        .and_then(|name| name.to_str())
        .map(sanitize_filename)
}

fn default_download_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .map(|home| home.join("downloads").join("qq"))
        .unwrap_or_else(|| PathBuf::from("downloads").join("qq"))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum QQAttachmentKind {
    Image,
    Document,
    Video,
    Voice,
}

impl QQAttachmentKind {
    fn from_marker(kind: &str) -> Option<Self> {
        match kind.trim().to_ascii_uppercase().as_str() {
            "IMAGE" | "PHOTO" => Some(Self::Image),
            "DOCUMENT" | "FILE" => Some(Self::Document),
            "VIDEO" => Some(Self::Video),
            "VOICE" => Some(Self::Voice),
            _ => None,
        }
    }

    fn qq_file_type(&self) -> Option<u8> {
        match self {
            Self::Image => Some(1),
            Self::Video => Some(2),
            Self::Voice => Some(3),
            Self::Document => Some(4),
        }
    }

    fn label(&self) -> &'static str {
        match self {
            Self::Image => "Image",
            Self::Document => "File",
            Self::Video => "Video",
            Self::Voice => "Voice",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct QQOutgoingAttachment {
    kind: QQAttachmentKind,
    target: String,
}

fn attachment_kind_from_metadata(attachment: &Value) -> Option<QQAttachmentKind> {
    let url = attachment.get("url").and_then(|u| u.as_str())?.trim();
    if url.is_empty() {
        return None;
    }

    let content_type = attachment
        .get("content_type")
        .and_then(|ct| ct.as_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    let filename = attachment
        .get("filename")
        .and_then(|f| f.as_str())
        .unwrap_or("");

    if content_type.starts_with("image/") || is_image_filename(filename) {
        return Some(QQAttachmentKind::Image);
    }
    if content_type.starts_with("video/") || is_video_filename(filename) {
        return Some(QQAttachmentKind::Video);
    }
    if content_type.starts_with("audio/")
        || content_type.contains("silk")
        || is_voice_filename(filename)
    {
        return Some(QQAttachmentKind::Voice);
    }

    Some(QQAttachmentKind::Document)
}

fn format_attachment_content(
    kind: QQAttachmentKind,
    local_filename: &str,
    local_path: &Path,
) -> String {
    match kind {
        QQAttachmentKind::Image => format!("[IMAGE:{}]", local_path.display()),
        _ => format!("[Document: {}] {}", local_filename, local_path.display()),
    }
}

fn fallback_remote_attachment_content(
    kind: QQAttachmentKind,
    attachment: &Value,
    url: &str,
) -> String {
    let filename = attachment
        .get("filename")
        .and_then(|f| f.as_str())
        .map(sanitize_filename)
        .or_else(|| filename_from_url(url))
        .unwrap_or_else(|| "attachment.bin".to_string());
    match kind {
        QQAttachmentKind::Image => format!("[IMAGE:{url}]"),
        _ => format!("[Document: {}] {}", filename, url),
    }
}

fn compose_message_text(text: &str, attachment_lines: &[String]) -> Option<String> {
    let text = text.trim();

    if text.is_empty() && attachment_lines.is_empty() {
        return None;
    }

    if text.is_empty() {
        return Some(attachment_lines.join("\n"));
    }

    if attachment_lines.is_empty() {
        return Some(text.to_string());
    }

    Some(format!("{text}\n\n{}", attachment_lines.join("\n")))
}

fn is_data_url(target: &str) -> bool {
    target.starts_with("data:")
}

fn parse_data_url_base64(target: &str) -> anyhow::Result<&str> {
    if !is_data_url(target) {
        anyhow::bail!("QQ rich-media target is not a data URL")
    }

    let Some((_, payload)) = target.split_once(";base64,") else {
        anyhow::bail!("QQ rich-media data URL must use ;base64,")
    };

    if payload.trim().is_empty() {
        anyhow::bail!("QQ rich-media data URL payload is empty")
    }

    Ok(payload)
}

fn expand_local_attachment_path(target: &str) -> PathBuf {
    PathBuf::from(shellexpand::tilde(target).into_owned())
}

fn parse_attachment_markers(message: &str) -> (String, Vec<QQOutgoingAttachment>) {
    let mut cleaned = String::with_capacity(message.len());
    let mut attachments = Vec::new();
    let mut cursor = 0usize;

    while let Some(rel_start) = message[cursor..].find('[') {
        let start = cursor + rel_start;
        cleaned.push_str(&message[cursor..start]);

        let Some(rel_end) = message[start..].find(']') else {
            cleaned.push_str(&message[start..]);
            cursor = message.len();
            break;
        };
        let end = start + rel_end;
        let marker_text = &message[start + 1..end];

        let parsed = marker_text.split_once(':').and_then(|(kind, target)| {
            let kind = QQAttachmentKind::from_marker(kind)?;
            let target = target.trim();
            if target.is_empty() {
                return None;
            }
            Some(QQOutgoingAttachment {
                kind,
                target: target.to_string(),
            })
        });

        if let Some(attachment) = parsed {
            attachments.push(attachment);
        } else {
            cleaned.push_str(&message[start..=end]);
        }

        cursor = end + 1;
    }

    if cursor < message.len() {
        cleaned.push_str(&message[cursor..]);
    }

    (cleaned.trim().to_string(), attachments)
}

fn build_attachment_notice(attachment: &QQOutgoingAttachment) -> String {
    let label = attachment.kind.label();
    let target = attachment.target.trim();
    if is_http_url(target) {
        format!("{label}: {target}")
    } else {
        format!("{label} saved on device: {target}")
    }
}

/// Deduplication set capacity — evict half of entries when full.
const DEDUP_CAPACITY: usize = 10_000;
const QQ_RECENT_ATTACHMENTS_LIMIT: usize = 20;
const QQ_RECENT_ATTACHMENTS_PROMPT_LIMIT: usize = 20;
const QQ_MAX_TEXT_MESSAGE_CHARS: usize = 4_000;
const QQ_DRAFT_UPDATE_INTERVAL_MS: u128 = 2_500;
const QQ_DRAFT_PLACEHOLDER: &str = "⏳ 正在思考…";

#[derive(Debug, Clone)]
struct QQDraftState {
    recipient: String,
    platform_message_id: String,
    last_sent_text: String,
    last_updated_at: Instant,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum QQProgressMode {
    FinalOnly,
    DraftRecall,
    StatusMessages,
}

impl QQProgressMode {
    fn from_config(value: Option<&str>) -> Self {
        match value.map(str::trim).filter(|value| !value.is_empty()) {
            Some(value) if value.eq_ignore_ascii_case("final-only") => Self::FinalOnly,
            Some(value) if value.eq_ignore_ascii_case("status-messages") => Self::StatusMessages,
            _ => Self::DraftRecall,
        }
    }

    fn supports_draft_updates(self) -> bool {
        matches!(self, Self::DraftRecall)
    }
}

/// QQ Official Bot channel — uses Tencent's official QQ Bot API with
/// OAuth2 authentication and a Discord-like WebSocket gateway protocol.
pub struct QQChannel {
    app_id: String,
    app_secret: String,
    allowed_users: Vec<String>,
    download_dir: PathBuf,
    progress_mode: QQProgressMode,
    /// Cached access token + expiry timestamp.
    token_cache: Arc<RwLock<Option<(String, u64)>>>,
    /// Message deduplication set.
    dedup: Arc<RwLock<HashSet<String>>>,
    /// Recent inbound attachments remembered per QQ chat.
    recent_attachments: Arc<RwLock<HashMap<String, VecDeque<String>>>>,
    /// Logical draft IDs mapped to the latest QQ platform message ID.
    drafts: Arc<RwLock<HashMap<String, QQDraftState>>>,
}

impl QQChannel {
    pub fn new(
        app_id: String,
        app_secret: String,
        allowed_users: Vec<String>,
        download_dir: Option<String>,
        progress_mode: Option<String>,
    ) -> Self {
        let download_dir = download_dir
            .filter(|value| !value.trim().is_empty())
            .map(|value| PathBuf::from(shellexpand::tilde(&value).into_owned()))
            .unwrap_or_else(default_download_dir);
        Self {
            app_id,
            app_secret,
            allowed_users,
            download_dir,
            progress_mode: QQProgressMode::from_config(progress_mode.as_deref()),
            token_cache: Arc::new(RwLock::new(None)),
            dedup: Arc::new(RwLock::new(HashSet::new())),
            recent_attachments: Arc::new(RwLock::new(HashMap::new())),
            drafts: Arc::new(RwLock::new(HashMap::new())),
        }
    }

    fn http_client(&self) -> reqwest::Client {
        crate::config::build_runtime_proxy_client("channel.qq")
    }

    fn is_user_allowed(&self, user_id: &str) -> bool {
        self.allowed_users.iter().any(|u| u == "*" || u == user_id)
    }

    fn incoming_dir(&self) -> PathBuf {
        self.download_dir.join("incoming")
    }

    fn sanitize_qq_user_id(raw: &str) -> String {
        raw.chars()
            .filter(|c| c.is_alphanumeric() || *c == '_')
            .collect()
    }

    fn message_url(recipient: &str) -> String {
        if let Some(group_id) = recipient.strip_prefix("group:") {
            format!("{QQ_API_BASE}/v2/groups/{group_id}/messages")
        } else {
            let raw_uid = recipient.strip_prefix("user:").unwrap_or(recipient);
            let user_id = Self::sanitize_qq_user_id(raw_uid);
            format!("{QQ_API_BASE}/v2/users/{user_id}/messages")
        }
    }

    fn file_url(recipient: &str) -> String {
        if let Some(group_id) = recipient.strip_prefix("group:") {
            format!("{QQ_API_BASE}/v2/groups/{group_id}/files")
        } else {
            let raw_uid = recipient.strip_prefix("user:").unwrap_or(recipient);
            let user_id = Self::sanitize_qq_user_id(raw_uid);
            format!("{QQ_API_BASE}/v2/users/{user_id}/files")
        }
    }

    fn recall_url(recipient: &str, message_id: &str) -> String {
        if let Some(group_id) = recipient.strip_prefix("group:") {
            format!("{QQ_API_BASE}/v2/groups/{group_id}/messages/{message_id}")
        } else {
            let raw_uid = recipient.strip_prefix("user:").unwrap_or(recipient);
            let user_id = Self::sanitize_qq_user_id(raw_uid);
            format!("{QQ_API_BASE}/v2/users/{user_id}/messages/{message_id}")
        }
    }

    fn extract_message_id(payload: &Value) -> Option<String> {
        payload
            .get("id")
            .and_then(Value::as_str)
            .or_else(|| payload.get("message_id").and_then(Value::as_str))
            .or_else(|| payload.get("messageId").and_then(Value::as_str))
            .filter(|value| !value.trim().is_empty())
            .map(ToString::to_string)
    }

    fn truncate_text(text: &str, max_chars: usize) -> String {
        let total_chars = text.chars().count();
        if total_chars <= max_chars {
            return text.to_string();
        }
        if max_chars == 0 {
            return String::new();
        }
        if max_chars == 1 {
            return "…".to_string();
        }

        let mut end = 0usize;
        for (count, (idx, ch)) in text.char_indices().enumerate() {
            if count >= max_chars - 1 {
                break;
            }
            end = idx + ch.len_utf8();
        }

        let mut truncated = text[..end].to_string();
        truncated.push('…');
        truncated
    }

    fn render_draft_text(text: &str) -> String {
        let stripped = super::strip_tool_call_tags(text);
        let (cleaned, _) = parse_attachment_markers(&stripped);
        let normalized = cleaned.trim();
        if normalized.is_empty() {
            return QQ_DRAFT_PLACEHOLDER.to_string();
        }

        Self::truncate_text(normalized, QQ_MAX_TEXT_MESSAGE_CHARS)
    }

    async fn recall_message(
        &self,
        recipient: &str,
        message_id: &str,
        token: &str,
    ) -> anyhow::Result<()> {
        if message_id.trim().is_empty() {
            return Ok(());
        }

        let url = Self::recall_url(recipient, message_id);
        ensure_https(&url)?;

        let resp = self
            .http_client()
            .delete(&url)
            .header("Authorization", format!("QQBot {token}"))
            .send()
            .await?;

        if !resp.status().is_success() {
            let status = resp.status();
            let err = resp.text().await.unwrap_or_default();
            anyhow::bail!("QQ recall message failed ({status}): {err}");
        }

        Ok(())
    }

    async fn fetch_attachment_bytes(&self, url: &str, token: &str) -> anyhow::Result<Vec<u8>> {
        ensure_https(url)?;

        let client = self.http_client();
        match client
            .get(url)
            .header("Authorization", format!("QQBot {token}"))
            .send()
            .await
        {
            Ok(resp) if resp.status().is_success() => {
                return Ok(resp.bytes().await?.to_vec());
            }
            Ok(resp) => {
                tracing::warn!(
                    url,
                    status = %resp.status(),
                    "QQ attachment fetch with auth failed; retrying without auth"
                );
            }
            Err(error) => {
                tracing::warn!(url, error = %error, "QQ attachment fetch with auth errored; retrying without auth");
            }
        }

        let resp = client.get(url).send().await?;
        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            anyhow::bail!("QQ attachment download failed ({status}): {body}");
        }

        Ok(resp.bytes().await?.to_vec())
    }

    async fn download_attachment_to_local(&self, attachment: &Value) -> Option<String> {
        let url = attachment.get("url").and_then(|u| u.as_str())?.trim();
        if url.is_empty() {
            return None;
        }

        let kind = attachment_kind_from_metadata(attachment)?;
        let token = match self.get_token().await {
            Ok(token) => token,
            Err(error) => {
                tracing::warn!(url, error = %error, "QQ attachment token fetch failed; using remote reference fallback");
                return Some(fallback_remote_attachment_content(kind, attachment, url));
            }
        };

        let bytes = match self.fetch_attachment_bytes(url, &token).await {
            Ok(bytes) => bytes,
            Err(error) => {
                tracing::warn!(url, error = %error, "QQ attachment download failed; using remote reference fallback");
                return Some(fallback_remote_attachment_content(kind, attachment, url));
            }
        };

        let save_dir = self.incoming_dir();
        if let Err(error) = tokio::fs::create_dir_all(&save_dir).await {
            tracing::warn!(path = %save_dir.display(), error = %error, "Failed to create QQ attachment directory; using remote reference fallback");
            return Some(fallback_remote_attachment_content(kind, attachment, url));
        }

        let filename = attachment
            .get("filename")
            .and_then(|f| f.as_str())
            .map(sanitize_filename)
            .filter(|value| !value.is_empty())
            .or_else(|| filename_from_url(url))
            .unwrap_or_else(|| match kind {
                QQAttachmentKind::Image => "image.png".to_string(),
                QQAttachmentKind::Video => "video.mp4".to_string(),
                QQAttachmentKind::Voice => "voice.silk".to_string(),
                QQAttachmentKind::Document => "attachment.bin".to_string(),
            });
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis();
        let local_path = save_dir.join(format!("{timestamp}_{filename}"));

        if let Err(error) = tokio::fs::write(&local_path, &bytes).await {
            tracing::warn!(path = %local_path.display(), error = %error, "Failed to save QQ attachment; using remote reference fallback");
            return Some(fallback_remote_attachment_content(kind, attachment, url));
        }

        Some(format_attachment_content(kind, &filename, &local_path))
    }

    async fn remember_recent_attachments(&self, chat_id: &str, attachment_lines: &[String]) {
        if attachment_lines.is_empty() {
            return;
        }

        let mut cache = self.recent_attachments.write().await;
        let entries = cache.entry(chat_id.to_string()).or_default();

        for line in attachment_lines.iter().rev() {
            if let Some(index) = entries.iter().position(|existing| existing == line) {
                entries.remove(index);
            }
            entries.push_front(line.clone());
        }

        while entries.len() > QQ_RECENT_ATTACHMENTS_LIMIT {
            entries.pop_back();
        }
    }

    async fn recent_attachments_for_prompt(&self, chat_id: &str) -> Vec<String> {
        let cache = self.recent_attachments.read().await;
        cache
            .get(chat_id)
            .map(|entries| {
                entries
                    .iter()
                    .take(QQ_RECENT_ATTACHMENTS_PROMPT_LIMIT)
                    .cloned()
                    .collect()
            })
            .unwrap_or_default()
    }

    fn render_recent_attachment_reference(attachment: &str) -> String {
        let trimmed = attachment.trim();
        if let Some(path) = trimmed
            .strip_prefix("[IMAGE:")
            .and_then(|value| value.strip_suffix(']'))
        {
            return format!("[Recent image attachment] {path}");
        }

        trimmed.to_string()
    }

    fn render_recent_attachment_context(recent_attachments: &[String]) -> Option<String> {
        if recent_attachments.is_empty() {
            return None;
        }

        let mut lines = vec![
            "[QQ attachment context]".to_string(),
            "Recent attachments in this chat (newest first):".to_string(),
        ];
        for (index, attachment) in recent_attachments.iter().enumerate() {
            let rendered = Self::render_recent_attachment_reference(attachment);
            lines.push(format!("{}. {}", index + 1, rendered));
        }
        lines.push(
            "These entries are saved attachment references only, not newly attached inline media for this turn. Use the listed path when the user refers to a previous file, image, video, or voice message from this chat."
                .to_string(),
        );

        Some(lines.join("\n"))
    }

    fn render_new_attachment_context() -> String {
        [
            "[QQ attachment context]",
            "This message includes newly received QQ attachments that have already been saved locally.",
            "Briefly confirm receipt once in your reply before handling the user's request.",
            "Use the local paths below for file operations and keep the acknowledgment short.",
        ]
        .join("\n")
    }

    async fn compose_message_content(&self, payload: &Value, chat_id: &str) -> Option<String> {
        let text = payload
            .get("content")
            .and_then(|c| c.as_str())
            .unwrap_or("");

        let mut attachment_lines = Vec::new();
        if let Some(attachments) = payload.get("attachments").and_then(|a| a.as_array()) {
            for attachment in attachments {
                if let Some(content) = self.download_attachment_to_local(attachment).await {
                    attachment_lines.push(content);
                }
            }
        }

        let base_content = compose_message_text(text, &attachment_lines)?;

        if !attachment_lines.is_empty() {
            self.remember_recent_attachments(chat_id, &attachment_lines)
                .await;
            return Some(format!(
                "{}\n\n{}",
                Self::render_new_attachment_context(),
                base_content
            ));
        }

        Some(base_content)
    }

    async fn send_text_message(
        &self,
        recipient: &str,
        content: &str,
        token: &str,
    ) -> anyhow::Result<Option<String>> {
        if content.trim().is_empty() {
            return Ok(None);
        }

        let url = Self::message_url(recipient);
        ensure_https(&url)?;

        let resp = self
            .http_client()
            .post(&url)
            .header("Authorization", format!("QQBot {token}"))
            .json(&json!({
                "content": content,
                "msg_type": 0,
            }))
            .send()
            .await?;

        let status = resp.status();
        let body = resp.bytes().await?;
        if !status.is_success() {
            let err = String::from_utf8_lossy(&body);
            anyhow::bail!("QQ send message failed ({status}): {err}");
        }

        if body.is_empty() {
            return Ok(None);
        }

        match serde_json::from_slice::<Value>(&body) {
            Ok(payload) => Ok(Self::extract_message_id(&payload)),
            Err(error) => {
                tracing::debug!(error = %error, "QQ send message returned a non-JSON success body");
                Ok(None)
            }
        }
    }

    async fn send_rich_media_message(
        &self,
        recipient: &str,
        attachment: &QQOutgoingAttachment,
        token: &str,
    ) -> anyhow::Result<Option<String>> {
        let target = attachment.target.trim();
        let effective_kind = normalize_rich_media_kind(attachment.kind, target);
        if effective_kind != attachment.kind {
            tracing::warn!(target = %target, original = %attachment.kind.label(), coerced = %effective_kind.label(), "QQ rich-media upload kind coerced for better compatibility");
        }
        let Some(file_type) = effective_kind.qq_file_type() else {
            anyhow::bail!(
                "QQ does not support native '{}' uploads on this endpoint",
                effective_kind.label()
            );
        };

        let mut upload_payload = json!({
            "file_type": file_type,
            "srv_send_msg": false,
        });

        if is_http_url(target) {
            ensure_https(target)?;
            upload_payload["url"] = Value::String(target.to_string());
        } else if is_data_url(target) {
            upload_payload["file_data"] = Value::String(parse_data_url_base64(target)?.to_string());
        } else {
            let local_path = expand_local_attachment_path(target);
            let bytes = tokio::fs::read(&local_path).await.map_err(|error| {
                anyhow::anyhow!(
                    "QQ rich-media local file read failed ({}): {}",
                    local_path.display(),
                    error
                )
            })?;
            if bytes.is_empty() {
                anyhow::bail!(
                    "QQ rich-media local file is empty: {}",
                    local_path.display()
                );
            }
            upload_payload["file_data"] = Value::String(STANDARD.encode(bytes));
            if effective_kind == QQAttachmentKind::Document {
                let file_name = local_path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .map(sanitize_filename)
                    .filter(|name| !name.is_empty())
                    .unwrap_or_else(|| "attachment.bin".to_string());
                upload_payload["file_name"] = Value::String(file_name);
            }
        }

        if effective_kind == QQAttachmentKind::Document && upload_payload.get("file_name").is_none()
        {
            if let Some(file_name) = filename_from_url(target) {
                upload_payload["file_name"] = Value::String(file_name);
            }
        }

        let upload_url = Self::file_url(recipient);
        ensure_https(&upload_url)?;
        let upload_resp = self
            .http_client()
            .post(&upload_url)
            .header("Authorization", format!("QQBot {token}"))
            .json(&upload_payload)
            .send()
            .await?;

        if !upload_resp.status().is_success() {
            let status = upload_resp.status();
            let err = upload_resp.text().await.unwrap_or_default();
            anyhow::bail!("QQ upload rich media failed ({status}): {err}");
        }

        let media: Value = upload_resp.json().await?;
        let message_url = Self::message_url(recipient);
        ensure_https(&message_url)?;
        let send_resp = self
            .http_client()
            .post(&message_url)
            .header("Authorization", format!("QQBot {token}"))
            .json(&json!({
                "msg_type": 7,
                "media": media,
            }))
            .send()
            .await?;

        let status = send_resp.status();
        let body = send_resp.bytes().await?;
        if !status.is_success() {
            let err = String::from_utf8_lossy(&body);
            anyhow::bail!("QQ send rich media failed ({status}): {err}");
        }

        if body.is_empty() {
            return Ok(None);
        }

        match serde_json::from_slice::<Value>(&body) {
            Ok(payload) => Ok(Self::extract_message_id(&payload)),
            Err(error) => {
                tracing::debug!(error = %error, "QQ rich-media send returned a non-JSON success body");
                Ok(None)
            }
        }
    }

    /// Fetch an access token from QQ's OAuth2 endpoint.
    async fn fetch_access_token(&self) -> anyhow::Result<(String, u64)> {
        let body = json!({
            "appId": self.app_id,
            "clientSecret": self.app_secret,
        });

        let resp = self
            .http_client()
            .post(QQ_AUTH_URL)
            .json(&body)
            .send()
            .await?;

        if !resp.status().is_success() {
            let status = resp.status();
            let err = resp.text().await.unwrap_or_default();
            anyhow::bail!("QQ token request failed ({status}): {err}");
        }

        let data: Value = resp.json().await?;
        let token = data
            .get("access_token")
            .and_then(|t| t.as_str())
            .ok_or_else(|| anyhow::anyhow!("Missing access_token in QQ response"))?
            .to_string();

        let expires_in = data
            .get("expires_in")
            .and_then(|e| e.as_str())
            .and_then(|e| e.parse::<u64>().ok())
            .unwrap_or(7200);

        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        // Expire 60 seconds early to avoid edge cases
        let expiry = now + expires_in.saturating_sub(60);

        Ok((token, expiry))
    }

    /// Get a valid access token, refreshing if expired.
    async fn get_token(&self) -> anyhow::Result<String> {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        {
            let cache = self.token_cache.read().await;
            if let Some((ref token, expiry)) = *cache {
                if now < expiry {
                    return Ok(token.clone());
                }
            }
        }

        let (token, expiry) = self.fetch_access_token().await?;
        {
            let mut cache = self.token_cache.write().await;
            *cache = Some((token.clone(), expiry));
        }
        Ok(token)
    }

    /// Get the WebSocket gateway URL.
    async fn get_gateway_url(&self, token: &str) -> anyhow::Result<String> {
        let resp = self
            .http_client()
            .get(format!("{QQ_API_BASE}/gateway"))
            .header("Authorization", format!("QQBot {token}"))
            .send()
            .await?;

        if !resp.status().is_success() {
            let status = resp.status();
            let err = resp.text().await.unwrap_or_default();
            anyhow::bail!("QQ gateway request failed ({status}): {err}");
        }

        let data: Value = resp.json().await?;
        let url = data
            .get("url")
            .and_then(|u| u.as_str())
            .ok_or_else(|| anyhow::anyhow!("Missing gateway URL in QQ response"))?
            .to_string();

        Ok(url)
    }

    /// Check and insert message ID for deduplication.
    async fn is_duplicate(&self, msg_id: &str) -> bool {
        if msg_id.is_empty() {
            return false;
        }

        let mut dedup = self.dedup.write().await;

        if dedup.contains(msg_id) {
            return true;
        }

        // Evict oldest half when at capacity
        if dedup.len() >= DEDUP_CAPACITY {
            let to_remove: Vec<String> = dedup.iter().take(DEDUP_CAPACITY / 2).cloned().collect();
            for key in to_remove {
                dedup.remove(&key);
            }
        }

        dedup.insert(msg_id.to_string());
        false
    }
}

#[async_trait]
impl Channel for QQChannel {
    fn name(&self) -> &str {
        "qq"
    }

    async fn send(&self, message: &SendMessage) -> anyhow::Result<()> {
        let token = self.get_token().await?;
        let raw_content = super::strip_tool_call_tags(&message.content);
        let (cleaned_content, attachments) = parse_attachment_markers(&raw_content);

        let mut text_lines = Vec::new();
        if !cleaned_content.trim().is_empty() {
            text_lines.push(cleaned_content.trim().to_string());
        }

        let mut rich_media = Vec::new();
        for attachment in attachments {
            if attachment.kind.qq_file_type().is_some() {
                rich_media.push(attachment);
            } else {
                text_lines.push(build_attachment_notice(&attachment));
            }
        }

        if !text_lines.is_empty() {
            self.send_text_message(&message.recipient, &text_lines.join("\n"), &token)
                .await?;
        }

        for attachment in rich_media {
            if let Err(error) = self
                .send_rich_media_message(&message.recipient, &attachment, &token)
                .await
            {
                tracing::warn!(
                    target = %attachment.target,
                    error = %error,
                    "QQ rich-media send failed; falling back to text notice"
                );
                self.send_text_message(
                    &message.recipient,
                    &build_attachment_notice(&attachment),
                    &token,
                )
                .await?;
            }
        }

        Ok(())
    }

    #[allow(clippy::too_many_lines)]
    async fn listen(&self, tx: tokio::sync::mpsc::Sender<ChannelMessage>) -> anyhow::Result<()> {
        tracing::info!("QQ: authenticating...");
        let token = self.get_token().await?;

        tracing::info!("QQ: fetching gateway URL...");
        let gw_url = self.get_gateway_url(&token).await?;

        tracing::info!("QQ: connecting to gateway WebSocket...");
        let (ws_stream, _) = tokio_tungstenite::connect_async(&gw_url).await?;
        let (mut write, mut read) = ws_stream.split();

        // Read Hello (opcode 10)
        let hello = read
            .next()
            .await
            .ok_or(anyhow::anyhow!("QQ: no hello frame"))??;
        let hello_data: Value = serde_json::from_str(&hello.to_string())?;
        let heartbeat_interval = hello_data
            .get("d")
            .and_then(|d| d.get("heartbeat_interval"))
            .and_then(Value::as_u64)
            .unwrap_or(41250);

        // Send Identify (opcode 2)
        // Intents: PUBLIC_GUILD_MESSAGES (1<<30) | C2C_MESSAGE_CREATE & GROUP_AT_MESSAGE_CREATE (1<<25)
        let intents: u64 = (1 << 25) | (1 << 30);
        let identify = json!({
            "op": 2,
            "d": {
                "token": format!("QQBot {token}"),
                "intents": intents,
                "properties": {
                    "os": "linux",
                    "browser": "zeroclaw",
                    "device": "zeroclaw",
                }
            }
        });
        write
            .send(Message::Text(identify.to_string().into()))
            .await?;

        tracing::info!("QQ: connected and identified");

        let mut sequence: i64 = -1;

        // Spawn heartbeat timer
        let (hb_tx, mut hb_rx) = tokio::sync::mpsc::channel::<()>(1);
        let hb_interval = heartbeat_interval;
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(std::time::Duration::from_millis(hb_interval));
            loop {
                interval.tick().await;
                if hb_tx.send(()).await.is_err() {
                    break;
                }
            }
        });

        loop {
            tokio::select! {
                _ = hb_rx.recv() => {
                    let d = if sequence >= 0 { json!(sequence) } else { json!(null) };
                    let hb = json!({"op": 1, "d": d});
                    if write
                        .send(Message::Text(hb.to_string().into()))
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
                msg = read.next() => {
                    let msg = match msg {
                        Some(Ok(Message::Text(t))) => t,
                        Some(Ok(Message::Close(_))) | None => break,
                        _ => continue,
                    };

                    let event: Value = match serde_json::from_str(msg.as_ref()) {
                        Ok(e) => e,
                        Err(_) => continue,
                    };

                    // Track sequence number
                    if let Some(s) = event.get("s").and_then(Value::as_i64) {
                        sequence = s;
                    }

                    let op = event.get("op").and_then(Value::as_u64).unwrap_or(0);

                    match op {
                        // Server requests immediate heartbeat
                        1 => {
                            let d = if sequence >= 0 { json!(sequence) } else { json!(null) };
                            let hb = json!({"op": 1, "d": d});
                            if write
                                .send(Message::Text(hb.to_string().into()))
                                .await
                                .is_err()
                            {
                                break;
                            }
                            continue;
                        }
                        // Reconnect
                        7 => {
                            tracing::warn!("QQ: received Reconnect (op 7)");
                            break;
                        }
                        // Invalid Session
                        9 => {
                            tracing::warn!("QQ: received Invalid Session (op 9)");
                            break;
                        }
                        _ => {}
                    }

                    // Only process dispatch events (op 0)
                    if op != 0 {
                        continue;
                    }

                    let event_type = event.get("t").and_then(|t| t.as_str()).unwrap_or("");
                    let d = match event.get("d") {
                        Some(d) => d,
                        None => continue,
                    };

                    match event_type {
                        "C2C_MESSAGE_CREATE" => {
                            let msg_id = d.get("id").and_then(|i| i.as_str()).unwrap_or("");
                            if self.is_duplicate(msg_id).await {
                                continue;
                            }

                            let author_id = d.get("author").and_then(|a| a.get("id")).and_then(|i| i.as_str()).unwrap_or("unknown");
                            // For QQ, user_openid is the identifier
                            let user_openid = d.get("author").and_then(|a| a.get("user_openid")).and_then(|u| u.as_str()).unwrap_or(author_id);

                            if !self.is_user_allowed(user_openid) {
                                tracing::warn!("QQ: ignoring C2C message from unauthorized user: {user_openid}");
                                continue;
                            }

                            let chat_id = format!("user:{user_openid}");

                            let Some(content) = self.compose_message_content(d, &chat_id).await else {
                                continue;
                            };

                            let channel_msg = ChannelMessage {
                                id: Uuid::new_v4().to_string(),
                                sender: user_openid.to_string(),
                                reply_target: chat_id,
                                content,
                                channel: "qq".to_string(),
                                timestamp: std::time::SystemTime::now()
                                    .duration_since(std::time::UNIX_EPOCH)
                                    .unwrap_or_default()
                                    .as_secs(),
                                thread_ts: None,
                            };

                            if tx.send(channel_msg).await.is_err() {
                                tracing::warn!("QQ: message channel closed");
                                break;
                            }
                        }
                        "GROUP_AT_MESSAGE_CREATE" => {
                            let msg_id = d.get("id").and_then(|i| i.as_str()).unwrap_or("");
                            if self.is_duplicate(msg_id).await {
                                continue;
                            }

                            let author_id = d.get("author").and_then(|a| a.get("member_openid")).and_then(|m| m.as_str()).unwrap_or("unknown");

                            if !self.is_user_allowed(author_id) {
                                tracing::warn!("QQ: ignoring group message from unauthorized user: {author_id}");
                                continue;
                            }

                            let group_openid = d.get("group_openid").and_then(|g| g.as_str()).unwrap_or("unknown");
                            let chat_id = format!("group:{group_openid}");

                            let Some(content) = self.compose_message_content(d, &chat_id).await else {
                                continue;
                            };

                            let channel_msg = ChannelMessage {
                                id: Uuid::new_v4().to_string(),
                                sender: author_id.to_string(),
                                reply_target: chat_id,
                                content,
                                channel: "qq".to_string(),
                                timestamp: std::time::SystemTime::now()
                                    .duration_since(std::time::UNIX_EPOCH)
                                    .unwrap_or_default()
                                    .as_secs(),
                                thread_ts: None,
                            };

                            if tx.send(channel_msg).await.is_err() {
                                tracing::warn!("QQ: message channel closed");
                                break;
                            }
                        }
                        _ => {}
                    }
                }
            }
        }

        anyhow::bail!("QQ WebSocket connection closed")
    }

    async fn health_check(&self) -> bool {
        self.fetch_access_token().await.is_ok()
    }

    fn supports_draft_updates(&self) -> bool {
        self.progress_mode.supports_draft_updates()
    }

    async fn send_draft(&self, message: &SendMessage) -> anyhow::Result<Option<String>> {
        let token = self.get_token().await?;
        let initial_text = Self::render_draft_text(&message.content);
        let Some(platform_message_id) = self
            .send_text_message(&message.recipient, &initial_text, &token)
            .await?
        else {
            tracing::debug!(recipient = %message.recipient, "QQ draft send returned no message ID; disabling draft streaming for this turn");
            return Ok(None);
        };

        let logical_draft_id = Uuid::new_v4().to_string();
        self.drafts.write().await.insert(
            logical_draft_id.clone(),
            QQDraftState {
                recipient: message.recipient.clone(),
                platform_message_id,
                last_sent_text: initial_text,
                last_updated_at: Instant::now(),
            },
        );

        Ok(Some(logical_draft_id))
    }

    async fn update_draft(
        &self,
        _recipient: &str,
        message_id: &str,
        text: &str,
    ) -> anyhow::Result<()> {
        let next_text = Self::render_draft_text(text);
        let state = { self.drafts.read().await.get(message_id).cloned() };

        let Some(state) = state else {
            return Ok(());
        };

        if state.last_sent_text == next_text {
            return Ok(());
        }

        if state.last_updated_at.elapsed().as_millis() < QQ_DRAFT_UPDATE_INTERVAL_MS {
            return Ok(());
        }

        let token = self.get_token().await?;
        let Some(new_platform_message_id) = self
            .send_text_message(&state.recipient, &next_text, &token)
            .await?
        else {
            tracing::debug!(recipient = %state.recipient, "QQ draft update returned no message ID; keeping previous draft visible");
            return Ok(());
        };

        if let Err(error) = self
            .recall_message(&state.recipient, &state.platform_message_id, &token)
            .await
        {
            tracing::debug!(message_id = %state.platform_message_id, error = %error, "QQ draft recall failed after replacement send");
        }

        if let Some(entry) = self.drafts.write().await.get_mut(message_id) {
            entry.platform_message_id = new_platform_message_id;
            entry.last_sent_text = next_text;
            entry.last_updated_at = Instant::now();
        }

        Ok(())
    }

    async fn finalize_draft(
        &self,
        recipient: &str,
        message_id: &str,
        text: &str,
    ) -> anyhow::Result<()> {
        let state = self.drafts.write().await.remove(message_id);

        if let Some(state) = state {
            let token = self.get_token().await?;
            if let Err(error) = self
                .recall_message(&state.recipient, &state.platform_message_id, &token)
                .await
            {
                tracing::debug!(message_id = %state.platform_message_id, error = %error, "QQ final draft recall failed");
            }
        }

        self.send(&SendMessage::new(text, recipient)).await
    }

    async fn cancel_draft(&self, _recipient: &str, message_id: &str) -> anyhow::Result<()> {
        let state = self.drafts.write().await.remove(message_id);

        let Some(state) = state else {
            return Ok(());
        };

        let token = self.get_token().await?;
        if let Err(error) = self
            .recall_message(&state.recipient, &state.platform_message_id, &token)
            .await
        {
            tracing::debug!(message_id = %state.platform_message_id, error = %error, "QQ cancel_draft recall failed");
        }

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_name() {
        let ch = QQChannel::new("id".into(), "secret".into(), vec![], None, None);
        assert_eq!(ch.name(), "qq");
    }

    #[test]
    fn test_user_allowed_wildcard() {
        let ch = QQChannel::new("id".into(), "secret".into(), vec!["*".into()], None, None);
        assert!(ch.is_user_allowed("anyone"));
    }

    #[test]
    fn test_user_allowed_specific() {
        let ch = QQChannel::new(
            "id".into(),
            "secret".into(),
            vec!["user123".into()],
            None,
            None,
        );
        assert!(ch.is_user_allowed("user123"));
        assert!(!ch.is_user_allowed("other"));
    }

    #[test]
    fn test_user_denied_empty() {
        let ch = QQChannel::new("id".into(), "secret".into(), vec![], None, None);
        assert!(!ch.is_user_allowed("anyone"));
    }

    #[tokio::test]
    async fn test_dedup() {
        let ch = QQChannel::new("id".into(), "secret".into(), vec![], None, None);
        assert!(!ch.is_duplicate("msg1").await);
        assert!(ch.is_duplicate("msg1").await);
        assert!(!ch.is_duplicate("msg2").await);
    }

    #[tokio::test]
    async fn test_dedup_empty_id() {
        let ch = QQChannel::new("id".into(), "secret".into(), vec![], None, None);
        // Empty IDs should never be considered duplicates
        assert!(!ch.is_duplicate("").await);
        assert!(!ch.is_duplicate("").await);
    }

    #[test]
    fn test_config_serde() {
        let toml_str = r#"
app_id = "12345"
app_secret = "secret_abc"
allowed_users = ["user1"]
download_dir = "~/downloads/qq"
"#;
        let config: crate::config::schema::QQConfig = toml::from_str(toml_str).unwrap();
        assert_eq!(config.app_id, "12345");
        assert_eq!(config.app_secret, "secret_abc");
        assert_eq!(config.allowed_users, vec!["user1"]);
        assert_eq!(config.download_dir.as_deref(), Some("~/downloads/qq"));
    }

    #[test]
    fn test_compose_message_text_text_only() {
        assert_eq!(
            compose_message_text("  hello world  ", &[]),
            Some("hello world".to_string())
        );
    }

    #[test]
    fn test_compose_message_text_attachment_only_image() {
        let attachments = vec!["[IMAGE:/tmp/a.jpg]".to_string()];
        assert_eq!(
            compose_message_text("   ", &attachments),
            Some("[IMAGE:/tmp/a.jpg]".to_string())
        );
    }

    #[test]
    fn test_compose_message_text_text_and_attachment_lines() {
        let attachments = vec![
            "[IMAGE:/tmp/a.png]".to_string(),
            "[Document: bundle.zip] /tmp/bundle.zip".to_string(),
        ];
        assert_eq!(
            compose_message_text("Here is an image", &attachments),
            Some(
                "Here is an image

[IMAGE:/tmp/a.png]
[Document: bundle.zip] /tmp/bundle.zip"
                    .to_string()
            )
        );
    }

    #[test]
    fn test_attachment_kind_from_metadata_defaults_to_document() {
        let payload = json!({
            "content_type": "application/zip",
            "filename": "bundle.zip",
            "url": "https://cdn.example.com/bundle.zip"
        });
        assert_eq!(
            attachment_kind_from_metadata(&payload),
            Some(QQAttachmentKind::Document)
        );
    }

    #[test]
    fn test_parse_attachment_markers_extracts_supported_markers() {
        let input = "Done
[IMAGE:/tmp/a.png]
[DOCUMENT:/tmp/bundle.zip]";
        let (cleaned, attachments) = parse_attachment_markers(input);
        assert_eq!(cleaned, "Done");
        assert_eq!(attachments.len(), 2);
        assert_eq!(attachments[0].kind, QQAttachmentKind::Image);
        assert_eq!(attachments[0].target, "/tmp/a.png");
        assert_eq!(attachments[1].kind, QQAttachmentKind::Document);
        assert_eq!(attachments[1].target, "/tmp/bundle.zip");
    }

    #[test]
    fn test_parse_data_url_base64_extracts_payload() {
        let payload = parse_data_url_base64("data:image/png;base64,aGVsbG8=").unwrap();
        assert_eq!(payload, "aGVsbG8=");
    }

    #[test]
    fn test_extract_message_id_supports_common_fields() {
        assert_eq!(
            QQChannel::extract_message_id(&json!({ "id": "msg-1" })),
            Some("msg-1".to_string())
        );
        assert_eq!(
            QQChannel::extract_message_id(&json!({ "message_id": "msg-2" })),
            Some("msg-2".to_string())
        );
        assert_eq!(
            QQChannel::extract_message_id(&json!({ "messageId": "msg-3" })),
            Some("msg-3".to_string())
        );
    }

    #[test]
    fn test_render_draft_text_strips_attachment_markers_and_truncates() {
        let rendered = QQChannel::render_draft_text(
            "Hello
[IMAGE:/tmp/sky.png]",
        );
        assert_eq!(rendered, "Hello");

        let long_text = "你".repeat(QQ_MAX_TEXT_MESSAGE_CHARS + 32);
        let truncated = QQChannel::render_draft_text(&long_text);
        assert_eq!(truncated.chars().count(), QQ_MAX_TEXT_MESSAGE_CHARS);
        assert!(truncated.ends_with('…'));
    }

    #[test]
    fn test_recall_url_matches_private_and_group_routes() {
        assert_eq!(
            QQChannel::recall_url("user:user_openid_123", "msg_1"),
            "https://api.sgroup.qq.com/v2/users/user_openid_123/messages/msg_1"
        );
        assert_eq!(
            QQChannel::recall_url("group:group_openid_456", "msg_2"),
            "https://api.sgroup.qq.com/v2/groups/group_openid_456/messages/msg_2"
        );
    }

    #[test]
    fn test_progress_mode_parsing_and_draft_support() {
        assert_eq!(
            QQProgressMode::from_config(None),
            QQProgressMode::DraftRecall
        );
        assert_eq!(
            QQProgressMode::from_config(Some("final-only")),
            QQProgressMode::FinalOnly
        );
        assert_eq!(
            QQProgressMode::from_config(Some("status-messages")),
            QQProgressMode::StatusMessages
        );
        assert!(QQProgressMode::DraftRecall.supports_draft_updates());
        assert!(!QQProgressMode::FinalOnly.supports_draft_updates());
        assert!(!QQProgressMode::StatusMessages.supports_draft_updates());
    }

    #[tokio::test]
    async fn test_send_rich_media_local_image_uses_file_data() {
        let temp = tempfile::tempdir().unwrap();
        let image_path = temp.path().join("sky.png");
        tokio::fs::write(&image_path, b"hello-image").await.unwrap();

        let attachment = QQOutgoingAttachment {
            kind: QQAttachmentKind::Image,
            target: image_path.display().to_string(),
        };
        let ch = QQChannel::new("id".into(), "secret".into(), vec![], None, None);

        let mut upload_payload = json!({
            "file_type": attachment.kind.qq_file_type().unwrap(),
            "srv_send_msg": false,
        });
        let bytes = tokio::fs::read(expand_local_attachment_path(&attachment.target))
            .await
            .unwrap();
        upload_payload["file_data"] = Value::String(STANDARD.encode(bytes));

        assert_eq!(
            upload_payload.get("file_type").and_then(Value::as_u64),
            Some(1)
        );
        assert_eq!(
            upload_payload.get("file_data").and_then(Value::as_str),
            Some("aGVsbG8taW1hZ2U=")
        );
        assert_eq!(ch.name(), "qq");
    }

    #[test]
    fn test_document_kind_has_qq_file_type() {
        assert_eq!(QQAttachmentKind::Document.qq_file_type(), Some(4));
    }

    #[tokio::test]
    async fn test_recent_attachment_cache_keeps_latest_unique_entries() {
        let ch = QQChannel::new("id".into(), "secret".into(), vec![], None, None);
        let chat_id = "user:test";

        ch.remember_recent_attachments(chat_id, &["[Document: a.zip] /tmp/a.zip".to_string()])
            .await;
        ch.remember_recent_attachments(chat_id, &["[Document: b.zip] /tmp/b.zip".to_string()])
            .await;
        ch.remember_recent_attachments(chat_id, &["[Document: a.zip] /tmp/a.zip".to_string()])
            .await;

        let recent = ch.recent_attachments_for_prompt(chat_id).await;
        assert_eq!(recent[0], "[Document: a.zip] /tmp/a.zip");
        assert_eq!(recent[1], "[Document: b.zip] /tmp/b.zip");
    }

    #[test]
    fn test_render_recent_attachment_context_lists_entries() {
        let context = QQChannel::render_recent_attachment_context(&[
            "[Document: a.zip] /tmp/a.zip".to_string(),
            "[IMAGE:/tmp/sky.png]".to_string(),
        ])
        .unwrap();
        assert!(context.contains("Recent attachments in this chat"));
        assert!(context.contains("1. [Document: a.zip] /tmp/a.zip"));
        assert!(context.contains("2. [Recent image attachment] /tmp/sky.png"));
        assert!(!context.contains("[IMAGE:/tmp/sky.png]"));
    }

    #[tokio::test]
    async fn test_recent_attachment_prompt_limit_matches_cache_limit() {
        let ch = QQChannel::new("id".into(), "secret".into(), vec![], None, None);
        let chat_id = "user:prompt-limit";

        for index in 0..25 {
            ch.remember_recent_attachments(
                chat_id,
                &[format!(
                    "[Document: file-{index}.txt] /tmp/file-{index}.txt"
                )],
            )
            .await;
        }

        let recent = ch.recent_attachments_for_prompt(chat_id).await;
        assert_eq!(recent.len(), QQ_RECENT_ATTACHMENTS_PROMPT_LIMIT);
        assert_eq!(
            QQ_RECENT_ATTACHMENTS_PROMPT_LIMIT,
            QQ_RECENT_ATTACHMENTS_LIMIT
        );
        assert_eq!(recent[0], "[Document: file-24.txt] /tmp/file-24.txt");
    }
}
