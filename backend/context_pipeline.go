package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/aliyun/aliyun-oss-go-sdk/oss"
)

const (
	clientProfileClaudeCode  = "claude_code"
	systemContextFilePrefix  = "qwen2api_context"
	systemContextPromptNote  = "System context files named qwen2api_context*.txt/.md/.json/.log may be attached. Use them as supporting context. User-uploaded files are separate user inputs and should also be respected."
	fileContentCacheTTL      = 15 * time.Minute
	defaultContextFileType   = "text/plain; charset=utf-8"
	minSessionAffinityTTL    = 60
	defaultContextUploadPoll = time.Second
)

var (
	workspaceCwdPatterns = []*regexp.Regexp{
		regexp.MustCompile(`(?i)<cwd>\s*([^\r\n<]+)\s*</cwd>`),
		regexp.MustCompile(`(?i)(?:cwd|workdir|workspace|working directory)\s*[:=]\s*([^\r\n]+)`),
		regexp.MustCompile(`(?i)(?:current directory|current project|project root)\s*[:=]\s*([^\r\n]+)`),
		regexp.MustCompile(`(?:当前目录|当前项目|项目根目录|工作目录|项目路径)\s*[:：]\s*([^\r\n]+)`),
	}
	fileCacheHintPatterns = []*regexp.Regexp{
		regexp.MustCompile(`(?i)file\s+unchanged\s+since\s+last\s+read`),
		regexp.MustCompile(`(?i)unchanged\s+since\s+last\s+read`),
		regexp.MustCompile(`(?i)refer\s+to\s+that\s+instead\s+of\s+re-?reading`),
		regexp.MustCompile(`(?i)still\s+current\s+[—-]\s+refer\s+to`),
	}
)

type UploadedLocalFileRecord struct {
	ID          string `json:"id"`
	Filename    string `json:"filename"`
	Size        int64  `json:"size"`
	ContentType string `json:"content_type"`
	CreatedAt   int64  `json:"created_at"`
	Path        string `json:"path"`
	OwnerToken  string `json:"owner_token"`
	Source      string `json:"source"`
	SHA256      string `json:"sha256"`
	Purpose     string `json:"purpose,omitempty"`
	Ephemeral   bool   `json:"ephemeral,omitempty"`
}

type NormalizedAttachment struct {
	FileID      string
	Filename    string
	ContentType string
	Source      string
	LocalPath   string
	SHA256      string
	Purpose     string
	RemoteRef   map[string]any
}

type PreprocessedAttachments struct {
	Payload         map[string]any
	Attachments     []NormalizedAttachment
	UploadedFileIDs []string
}

type LocalContextFile struct {
	Filename    string
	Ext         string
	ContentType string
	Text        string
	SHA256      string
}

type ContextOffloadPlan struct {
	Mode               string
	InlineMessages     []any
	GeneratedFiles     []LocalContextFile
	SummaryText        string
	EstimatedPromptLen int
	Note               string
}

type UpstreamFileCacheEntry struct {
	SessionKey     string         `json:"session_key"`
	AccountEmail   string         `json:"account_email"`
	SHA256         string         `json:"sha256"`
	Ext            string         `json:"ext"`
	Filename       string         `json:"filename"`
	RemoteFileMeta map[string]any `json:"remote_file_meta"`
	CreatedAt      int64          `json:"created_at"`
	ExpiresAt      int64          `json:"expires_at"`
}

type SessionAffinityRecord struct {
	SessionKey    string           `json:"session_key"`
	Surface       string           `json:"surface"`
	AccountEmail  string           `json:"account_email"`
	UploadedFiles []map[string]any `json:"uploaded_files"`
	ChatID        string           `json:"chat_id,omitempty"`
	MessageHashes []string         `json:"message_hashes,omitempty"`
	UpdatedAt     int64            `json:"updated_at"`
	ExpiresAt     int64            `json:"expires_at"`
}

type PreparedRequestContext struct {
	Payload            map[string]any
	SessionKey         string
	ContextMode        string
	UpstreamFiles      []map[string]any
	BoundAccount       *Account
	BoundAccountEmail  string
	WorkspaceRoot      string
	AttachmentFallback bool
}

type toolCallRef struct {
	Name     string
	FilePath string
}

type cachedFileContent struct {
	Content   string
	ExpiresAt time.Time
}

type fileContentCache struct {
	mu    sync.Mutex
	items map[string]cachedFileContent
}

func newFileContentCache() *fileContentCache {
	return &fileContentCache{items: map[string]cachedFileContent{}}
}

func (c *fileContentCache) key(apiToken, filePath string) string {
	return strings.TrimSpace(apiToken) + "::" + normalizeCachePath(filePath)
}

func (c *fileContentCache) Put(apiToken, filePath, content string) {
	if c == nil || strings.TrimSpace(filePath) == "" || isFileCacheHint(content) || strings.TrimSpace(content) == "" {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	now := time.Now()
	for key, item := range c.items {
		if !item.ExpiresAt.IsZero() && now.After(item.ExpiresAt) {
			delete(c.items, key)
		}
	}
	c.items[c.key(apiToken, filePath)] = cachedFileContent{
		Content:   content,
		ExpiresAt: now.Add(fileContentCacheTTL),
	}
}

func (c *fileContentCache) Get(apiToken, filePath string) (string, bool) {
	if c == nil || strings.TrimSpace(filePath) == "" {
		return "", false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	key := c.key(apiToken, filePath)
	item, ok := c.items[key]
	if !ok {
		return "", false
	}
	if !item.ExpiresAt.IsZero() && time.Now().After(item.ExpiresAt) {
		delete(c.items, key)
		return "", false
	}
	return item.Content, true
}

func normalizeCachePath(path string) string {
	return strings.ToLower(strings.ReplaceAll(strings.TrimSpace(path), "\\", "/"))
}

func isFileCacheHint(text string) bool {
	if strings.TrimSpace(text) == "" || len(text) > 500 {
		return false
	}
	for _, pattern := range fileCacheHintPatterns {
		if pattern.MatchString(text) {
			return true
		}
	}
	return false
}

func detectClientProfile(r *http.Request, tools []map[string]any) string {
	if r != nil {
		ua := strings.ToLower(strings.TrimSpace(r.UserAgent()))
		switch {
		case strings.Contains(ua, "claude-cli"),
			strings.Contains(ua, "claude code"),
			strings.Contains(ua, "codex"),
			strings.Contains(ua, "cursor"):
			return clientProfileClaudeCode
		}
	}
	if len(tools) > 0 {
		return clientProfileClaudeCode
	}
	return ""
}

func buildWorkspaceNotice(workspaceRoot string) string {
	return strings.Join([]string{
		"[WORKSPACE CONTEXT]",
		"When the user says current directory, current project, or workspace, use the client tool runtime's current working directory.",
		"Do not invent or force absolute roots such as /workspace, /app, Desktop, Temp, or a repository path that was not provided by the user or a recent tool result.",
		"Prefer relative paths for Read/Write/Edit/Grep/Glob and shell commands. Use an absolute path only when the user explicitly provided it or a recent tool result proved it.",
		"Container storage paths and server process paths are internal implementation details, not the user's requested working directory.",
		"[/WORKSPACE CONTEXT]",
	}, "\n")
}

func deriveWorkspaceRoot(payload map[string]any) string {
	if explicit := strings.TrimSpace(anyString(payload["_workspace_root"], "")); explicit != "" {
		return normalizeWorkspacePath(explicit)
	}
	if explicit := strings.TrimSpace(anyString(payload["workspace_root"], "")); explicit != "" {
		return normalizeWorkspacePath(explicit)
	}
	texts := payloadTextFragments(payload)
	type candidate struct {
		Score int
		Path  string
	}
	best := candidate{}
	for _, text := range texts {
		for _, pattern := range workspaceCwdPatterns {
			for _, match := range pattern.FindAllStringSubmatch(text, -1) {
				if len(match) < 2 {
					continue
				}
				root := projectRootFor(cleanWorkspaceCandidate(match[1]))
				score := projectScore(root) + 10
				if score > best.Score {
					best = candidate{Score: score, Path: root}
				}
			}
		}
	}
	if best.Path != "" {
		return normalizeWorkspacePath(best.Path)
	}
	return ""
}

func payloadTextFragments(payload map[string]any) []string {
	out := []string{}
	if system := payload["system"]; system != nil {
		out = append(out, flattenContentText(system))
	}
	for _, raw := range anyList(payload["messages"]) {
		msg, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if text := flattenContentText(msg["content"]); text != "" {
			out = append(out, text)
		}
	}
	return out
}

func cleanWorkspaceCandidate(value string) string {
	value = strings.TrimSpace(value)
	value = strings.Trim(value, "`'\"")
	value = strings.TrimRight(value, ".,;，。；:：)]}>")
	if idx := strings.IndexAny(value, "\r\n"); idx >= 0 {
		value = value[:idx]
	}
	return strings.TrimSpace(value)
}

func projectRootFor(pathText string) string {
	pathText = strings.TrimSpace(pathText)
	if pathText == "" {
		return ""
	}
	abs, err := filepath.Abs(pathText)
	if err != nil {
		return pathText
	}
	probe := abs
	info, err := os.Stat(abs)
	if err == nil && !info.IsDir() {
		probe = filepath.Dir(abs)
	}
	current := probe
	for {
		if hasProjectMarkers(current) {
			return current
		}
		parent := filepath.Dir(current)
		if parent == current {
			break
		}
		current = parent
	}
	return probe
}

func hasProjectMarkers(dir string) bool {
	markerSets := [][]string{
		{"backend", "frontend"},
		{"backend", "go.mod"},
		{"frontend", "package.json"},
		{".git"},
	}
	for _, markers := range markerSets {
		ok := true
		for _, marker := range markers {
			if _, err := os.Stat(filepath.Join(dir, marker)); err != nil {
				ok = false
				break
			}
		}
		if ok {
			return true
		}
	}
	for _, marker := range []string{"go.mod", "pyproject.toml", "package.json", ".git"} {
		if _, err := os.Stat(filepath.Join(dir, marker)); err == nil {
			return true
		}
	}
	return false
}

func projectScore(pathText string) int {
	if strings.TrimSpace(pathText) == "" {
		return 0
	}
	score := 1
	info, err := os.Stat(pathText)
	if err == nil {
		score += 4
		if info.IsDir() {
			score += 3
		}
	}
	for _, marker := range []string{".git", "backend", "frontend", "go.mod", "pyproject.toml", "package.json"} {
		if _, err := os.Stat(filepath.Join(pathText, marker)); err == nil {
			score += 2
		}
	}
	return score
}

func normalizeWorkspacePath(pathText string) string {
	if strings.TrimSpace(pathText) == "" {
		return ""
	}
	if abs, err := filepath.Abs(pathText); err == nil {
		return filepath.Clean(abs)
	}
	return filepath.Clean(pathText)
}

func injectWorkspaceNotice(payload map[string]any, workspaceRoot string) map[string]any {
	notice := buildWorkspaceNotice(workspaceRoot)
	if notice == "" {
		return payload
	}
	messages := anyList(payload["messages"])
	for _, raw := range messages {
		msg, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if role := stringValue(msg, "role", ""); role != "system" && role != "developer" {
			continue
		}
		text := flattenContentText(msg["content"])
		if strings.Contains(text, "[WORKSPACE ROOT - MUST OBEY]") || strings.Contains(text, "[WORKSPACE CONTEXT]") {
			return payload
		}
	}
	rewritten := deepCopyMap(payload)
	prefixed := []any{map[string]any{"role": "system", "content": notice}}
	prefixed = append(prefixed, anyList(rewritten["messages"])...)
	rewritten["messages"] = prefixed
	return rewritten
}

func deepCopyMap(src map[string]any) map[string]any {
	if src == nil {
		return map[string]any{}
	}
	raw, _ := json.Marshal(src)
	var dst map[string]any
	_ = json.Unmarshal(raw, &dst)
	if dst == nil {
		dst = map[string]any{}
	}
	return dst
}

func flattenContentText(content any) string {
	switch v := content.(type) {
	case nil:
		return ""
	case string:
		return v
	case []any:
		parts := []string{}
		for _, item := range v {
			switch x := item.(type) {
			case string:
				if strings.TrimSpace(x) != "" {
					parts = append(parts, x)
				}
			case map[string]any:
				switch stringValue(x, "type", "") {
				case "text", "input_text", "output_text":
					if text := stringValue(x, "text", ""); strings.TrimSpace(text) != "" {
						parts = append(parts, text)
					}
				case "tool_result", "function_call_output":
					if text := flattenContentText(x["content"]); strings.TrimSpace(text) != "" {
						parts = append(parts, text)
					}
				}
			}
		}
		return strings.Join(parts, "\n")
	case map[string]any:
		for _, key := range []string{"text", "content", "output"} {
			if text := flattenContentText(v[key]); text != "" {
				return text
			}
		}
		raw, _ := json.Marshal(v)
		return string(raw)
	default:
		return fmt.Sprint(v)
	}
}

func (app *App) saveLocalBytes(filename, contentType string, raw []byte, source, purpose, ownerToken string, ephemeral bool) (UploadedLocalFileRecord, error) {
	if app == nil {
		return UploadedLocalFileRecord{}, fmt.Errorf("app is nil")
	}
	if err := os.MkdirAll(app.settings.ContextGeneratedDir, 0o755); err != nil {
		return UploadedLocalFileRecord{}, err
	}
	base := filepath.Base(strings.TrimSpace(filename))
	if base == "." || base == string(filepath.Separator) || base == "" {
		base = "attachment.bin"
	}
	id := "file-" + randomID()[:24]
	path := filepath.Join(app.settings.ContextGeneratedDir, id+"-"+base)
	if err := os.WriteFile(path, raw, 0o644); err != nil {
		return UploadedLocalFileRecord{}, err
	}
	if strings.TrimSpace(contentType) == "" {
		contentType = mime.TypeByExtension(filepath.Ext(base))
	}
	if strings.TrimSpace(contentType) == "" {
		contentType = "application/octet-stream"
	}
	sum := sha256.Sum256(raw)
	record := UploadedLocalFileRecord{
		ID:          id,
		Filename:    base,
		Size:        int64(len(raw)),
		ContentType: contentType,
		CreatedAt:   time.Now().Unix(),
		Path:        path,
		OwnerToken:  ownerToken,
		Source:      source,
		SHA256:      hex.EncodeToString(sum[:]),
		Purpose:     purpose,
		Ephemeral:   ephemeral,
	}
	records, err := app.loadUploadedLocalFiles()
	if err != nil {
		return UploadedLocalFileRecord{}, err
	}
	records = append(records, record)
	if err := app.saveUploadedLocalFiles(records); err != nil {
		return UploadedLocalFileRecord{}, err
	}
	return record, nil
}

func (app *App) loadUploadedLocalFiles() ([]UploadedLocalFileRecord, error) {
	records := []UploadedLocalFileRecord{}
	if app == nil || app.uploadedFileStore == nil {
		return records, nil
	}
	if err := app.uploadedFileStore.LoadInto(&records); err != nil {
		return nil, err
	}
	return records, nil
}

func (app *App) saveUploadedLocalFiles(records []UploadedLocalFileRecord) error {
	if app == nil || app.uploadedFileStore == nil {
		return nil
	}
	return app.uploadedFileStore.Save(records)
}

func (app *App) loadContextCacheEntries() ([]UpstreamFileCacheEntry, error) {
	records := []UpstreamFileCacheEntry{}
	if app == nil || app.contextCacheStore == nil {
		return records, nil
	}
	if err := app.contextCacheStore.LoadInto(&records); err != nil {
		return nil, err
	}
	return records, nil
}

func (app *App) saveContextCacheEntries(records []UpstreamFileCacheEntry) error {
	if app == nil || app.contextCacheStore == nil {
		return nil
	}
	return app.contextCacheStore.Save(records)
}

func (app *App) loadSessionAffinityRecords() ([]SessionAffinityRecord, error) {
	records := []SessionAffinityRecord{}
	if app == nil || app.sessionStore == nil {
		return records, nil
	}
	if err := app.sessionStore.LoadInto(&records); err != nil {
		return nil, err
	}
	return records, nil
}

func (app *App) saveSessionAffinityRecords(records []SessionAffinityRecord) error {
	if app == nil || app.sessionStore == nil {
		return nil
	}
	return app.sessionStore.Save(records)
}

func (app *App) getUploadedLocalFile(fileID, ownerToken string) (*UploadedLocalFileRecord, error) {
	records, err := app.loadUploadedLocalFiles()
	if err != nil {
		return nil, err
	}
	for _, record := range records {
		if record.ID != fileID {
			continue
		}
		if record.OwnerToken != "" && ownerToken != "" && record.OwnerToken != ownerToken {
			return nil, fmt.Errorf("file %s is owned by another token", fileID)
		}
		rec := record
		return &rec, nil
	}
	return nil, nil
}

func (app *App) deleteUploadedLocalFileRecord(fileID string) error {
	records, err := app.loadUploadedLocalFiles()
	if err != nil {
		return err
	}
	next := records[:0]
	for _, record := range records {
		if record.ID != fileID {
			next = append(next, record)
		}
	}
	return app.saveUploadedLocalFiles(next)
}

func decodeDataURI(uri string) (string, []byte, error) {
	parts := strings.SplitN(uri, ",", 2)
	if len(parts) != 2 {
		return "", nil, fmt.Errorf("invalid data uri")
	}
	header := parts[0]
	contentType := "application/octet-stream"
	if strings.HasPrefix(header, "data:") {
		contentType = strings.TrimPrefix(header, "data:")
		if idx := strings.Index(contentType, ";"); idx >= 0 {
			contentType = contentType[:idx]
		}
	}
	raw, err := base64.StdEncoding.DecodeString(parts[1])
	if err != nil {
		return "", nil, err
	}
	return contentType, raw, nil
}

func extractInlineFilePayload(block map[string]any) (string, string, []byte, bool, error) {
	filename := firstNonEmpty(stringValue(block, "filename", ""), stringValue(block, "name", ""), "attachment.txt")
	contentType := firstNonEmpty(stringValue(block, "mime_type", ""), stringValue(block, "content_type", ""), "text/plain")
	if text := stringValue(block, "text", ""); text != "" {
		return filename, contentType, []byte(text), true, nil
	}
	if content := stringValue(block, "content", ""); content != "" && !strings.HasPrefix(strings.TrimSpace(content), "data:") {
		return filename, contentType, []byte(content), true, nil
	}
	if encoded := stringValue(block, "data_base64", ""); encoded != "" {
		raw, err := base64.StdEncoding.DecodeString(encoded)
		return filename, contentType, raw, true, err
	}
	if encoded := stringValue(block, "data", ""); encoded != "" {
		raw, err := base64.StdEncoding.DecodeString(encoded)
		return filename, contentType, raw, true, err
	}
	if content := stringValue(block, "content", ""); strings.HasPrefix(strings.TrimSpace(content), "data:") {
		decodedType, raw, err := decodeDataURI(content)
		if err != nil {
			return "", "", nil, true, err
		}
		return filename, decodedType, raw, true, nil
	}
	return "", "", nil, false, nil
}

func (app *App) preprocessAttachments(payload map[string]any, ownerToken string) (PreprocessedAttachments, error) {
	rewritten := deepCopyMap(payload)
	out := PreprocessedAttachments{Payload: rewritten}
	for msgIndex, rawMsg := range anyList(rewritten["messages"]) {
		msg, ok := rawMsg.(map[string]any)
		if !ok {
			continue
		}
		contentList, ok := msg["content"].([]any)
		if !ok {
			continue
		}
		for partIndex, rawPart := range contentList {
			part, ok := rawPart.(map[string]any)
			if !ok {
				continue
			}
			partType := stringValue(part, "type", "")
			switch partType {
			case "image_url":
				imageURL, _ := part["image_url"].(map[string]any)
				urlText := strings.TrimSpace(anyString(firstNonNil(imageURL["url"], part["url"]), ""))
				if !strings.HasPrefix(urlText, "data:") {
					continue
				}
				contentType, bytesValue, err := decodeDataURI(urlText)
				if err != nil {
					return PreprocessedAttachments{}, err
				}
				record, err := app.saveLocalBytes("inline-image", contentType, bytesValue, "inline-image", "user-upload", ownerToken, true)
				if err != nil {
					return PreprocessedAttachments{}, err
				}
				out.UploadedFileIDs = append(out.UploadedFileIDs, record.ID)
				out.Attachments = append(out.Attachments, NormalizedAttachment{
					FileID:      record.ID,
					Filename:    record.Filename,
					ContentType: record.ContentType,
					Source:      "inline-image",
					LocalPath:   record.Path,
					SHA256:      record.SHA256,
					Purpose:     "user-upload",
				})
				contentList[partIndex] = map[string]any{"type": "input_image", "file_id": record.ID, "mime_type": contentType, "filename": record.Filename}
			case "input_file", "file":
				if existingFileID := strings.TrimSpace(anyString(part["file_id"], "")); existingFileID != "" {
					record, err := app.getUploadedLocalFile(existingFileID, ownerToken)
					if err != nil {
						return PreprocessedAttachments{}, err
					}
					if record != nil {
						out.Attachments = append(out.Attachments, NormalizedAttachment{
							FileID:      record.ID,
							Filename:    record.Filename,
							ContentType: record.ContentType,
							Source:      firstNonEmpty(record.Source, "upload-ref"),
							LocalPath:   record.Path,
							SHA256:      record.SHA256,
							Purpose:     firstNonEmpty(record.Purpose, "user-upload"),
						})
						contentList[partIndex] = map[string]any{
							"type":      "input_file",
							"file_id":   record.ID,
							"filename":  record.Filename,
							"mime_type": record.ContentType,
						}
					}
					continue
				}
				filename, contentType, raw, ok, err := extractInlineFilePayload(part)
				if err != nil {
					return PreprocessedAttachments{}, err
				}
				if !ok {
					continue
				}
				record, err := app.saveLocalBytes(filename, contentType, raw, "inline-file", "user-upload", ownerToken, true)
				if err != nil {
					return PreprocessedAttachments{}, err
				}
				out.UploadedFileIDs = append(out.UploadedFileIDs, record.ID)
				out.Attachments = append(out.Attachments, NormalizedAttachment{
					FileID:      record.ID,
					Filename:    record.Filename,
					ContentType: record.ContentType,
					Source:      "inline-file",
					LocalPath:   record.Path,
					SHA256:      record.SHA256,
					Purpose:     "user-upload",
				})
				contentList[partIndex] = map[string]any{
					"type":      "input_file",
					"file_id":   record.ID,
					"filename":  record.Filename,
					"mime_type": record.ContentType,
				}
			}
		}
		msg["content"] = contentList
		anyListValue := anyList(rewritten["messages"])
		anyListValue[msgIndex] = msg
		rewritten["messages"] = anyListValue
	}
	return out, nil
}

func estimatePromptLen(messages []any, tools []map[string]any, clientProfile string) int {
	total := 0
	for _, raw := range messages {
		msg, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		total += len(flattenContentText(msg["content"])) + 24
	}
	for _, tool := range tools {
		total += len(stringValue(tool, "name", "")) + len(stringValue(tool, "description", ""))
	}
	if clientProfile == clientProfileClaudeCode {
		total += 512
	}
	return total
}

func makeContextFile(text string) LocalContextFile {
	sum := sha256.Sum256([]byte(text))
	return LocalContextFile{
		Filename:    systemContextFilePrefix + "_history.txt",
		Ext:         "txt",
		ContentType: defaultContextFileType,
		Text:        text,
		SHA256:      hex.EncodeToString(sum[:]),
	}
}

func planContextOffload(settings Settings, messages []any, tools []map[string]any, clientProfile string) ContextOffloadPlan {
	estimated := estimatePromptLen(messages, tools, clientProfile)
	if estimated <= settings.ContextInlineMaxChars {
		return ContextOffloadPlan{Mode: "inline", InlineMessages: messages, EstimatedPromptLen: estimated}
	}
	latestUserText := ""
	for i := len(messages) - 1; i >= 0; i-- {
		msg, ok := messages[i].(map[string]any)
		if !ok || stringValue(msg, "role", "") != "user" {
			continue
		}
		latestUserText = flattenContentText(msg["content"])
		break
	}
	older := messages
	if len(messages) > 0 {
		older = messages[:len(messages)-1]
	}
	var serialized []string
	for idx, raw := range older {
		msg, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		text := strings.TrimSpace(flattenContentText(msg["content"]))
		if text == "" {
			continue
		}
		serialized = append(serialized, fmt.Sprintf("## Message %d [%s]\n%s\n", idx+1, firstNonEmpty(stringValue(msg, "role", ""), "unknown"), text))
	}
	attachmentText := strings.TrimSpace(strings.Join(serialized, "\n"))
	if attachmentText == "" {
		return ContextOffloadPlan{Mode: "inline", InlineMessages: messages, EstimatedPromptLen: estimated}
	}
	mode := "hybrid"
	if estimated > settings.ContextForceFileMaxChars {
		mode = "file"
	}
	rewrittenText := strings.TrimSpace(latestUserText)
	if rewrittenText != "" {
		rewrittenText += "\n\n" + systemContextPromptNote
	} else {
		rewrittenText = systemContextPromptNote
	}
	return ContextOffloadPlan{
		Mode:               mode,
		InlineMessages:     []any{map[string]any{"role": "user", "content": rewrittenText}},
		GeneratedFiles:     []LocalContextFile{makeContextFile(attachmentText)},
		SummaryText:        trim(attachmentText, 1200),
		EstimatedPromptLen: estimated,
		Note:               systemContextPromptNote,
	}
}

func deriveSessionKey(surface, authToken string, payload map[string]any) string {
	if explicit := strings.TrimSpace(anyString(payload["session_key"], "")); explicit != "" {
		return explicit
	}
	if explicit := strings.TrimSpace(anyString(payload["conversation_id"], "")); explicit != "" {
		return explicit
	}
	if meta, ok := payload["metadata"].(map[string]any); ok {
		if explicit := strings.TrimSpace(anyString(meta["conversation_id"], "")); explicit != "" {
			return explicit
		}
	}
	firstUserText := ""
	for _, raw := range anyList(payload["messages"]) {
		msg, ok := raw.(map[string]any)
		if !ok || stringValue(msg, "role", "") != "user" {
			continue
		}
		firstUserText = flattenContentText(msg["content"])
		if strings.TrimSpace(firstUserText) != "" {
			break
		}
	}
	sum := sha256.Sum256([]byte(surface + "::" + authToken + "::" + anyString(payload["model"], "") + "::" + trim(firstUserText, 400)))
	return hex.EncodeToString(sum[:])[:24]
}

func (app *App) loadSessionRecord(sessionKey string) (*SessionAffinityRecord, []SessionAffinityRecord, error) {
	records, err := app.loadSessionAffinityRecords()
	if err != nil {
		return nil, nil, err
	}
	now := time.Now().Unix()
	filtered := records[:0]
	var found *SessionAffinityRecord
	for _, record := range records {
		if record.ExpiresAt > 0 && record.ExpiresAt < now {
			continue
		}
		filtered = append(filtered, record)
		if record.SessionKey == sessionKey {
			copyRecord := record
			found = &copyRecord
		}
	}
	if len(filtered) != len(records) {
		_ = app.saveSessionAffinityRecords(filtered)
	}
	return found, filtered, nil
}

func upsertSessionRecord(records []SessionAffinityRecord, next SessionAffinityRecord) []SessionAffinityRecord {
	for idx := range records {
		if records[idx].SessionKey == next.SessionKey {
			records[idx] = next
			return records
		}
	}
	return append(records, next)
}

func dedupeUpstreamFiles(files []map[string]any) []map[string]any {
	seen := map[string]bool{}
	out := make([]map[string]any, 0, len(files))
	for _, file := range files {
		key := firstNonEmpty(anyString(file["id"], ""), anyString(file["url"], ""), mustJSON(file))
		if key == "" || seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, file)
	}
	return out
}

func findUpstreamCacheEntry(entries []UpstreamFileCacheEntry, sessionKey, accountEmail, sha, ext string) *UpstreamFileCacheEntry {
	now := time.Now().Unix()
	for _, entry := range entries {
		if entry.ExpiresAt > 0 && entry.ExpiresAt < now {
			continue
		}
		if entry.SessionKey == sessionKey && entry.AccountEmail == accountEmail && entry.SHA256 == sha && strings.EqualFold(entry.Ext, ext) {
			copyEntry := entry
			return &copyEntry
		}
	}
	return nil
}

func normalizeSignRegion(region string) string {
	region = strings.TrimSpace(region)
	return strings.TrimPrefix(region, "oss-")
}

func upstreamFileClass(contentType string) string {
	lowered := strings.ToLower(strings.TrimSpace(contentType))
	switch {
	case strings.HasPrefix(lowered, "image/"):
		return "image"
	case strings.HasPrefix(lowered, "audio/"):
		return "audio"
	case strings.HasPrefix(lowered, "video/"):
		return "video"
	default:
		return "document"
	}
}

func (app *App) uploadLocalFileToUpstream(ctx context.Context, acc *Account, local UploadedLocalFileRecord) (map[string]any, error) {
	if app == nil || app.client == nil || acc == nil {
		return nil, fmt.Errorf("upload prerequisites missing")
	}
	raw, err := os.ReadFile(local.Path)
	if err != nil {
		return nil, err
	}
	contentType := firstNonEmpty(local.ContentType, mime.TypeByExtension(filepath.Ext(local.Filename)), "application/octet-stream")
	status, text, err := app.client.requestJSON(ctx, http.MethodPost, "/api/v2/files/getstsToken", qwenCreds(acc.Token, acc.Cookies), map[string]any{
		"filename": local.Filename,
		"filesize": len(raw),
		"filetype": "file",
	}, 20*time.Second)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("getstsToken failed: %d %s", status, truncate(text, 200))
	}
	var stsPayload map[string]any
	if err := json.Unmarshal([]byte(text), &stsPayload); err != nil {
		return nil, err
	}
	stsData, _ := stsPayload["data"].(map[string]any)
	fileID := anyString(stsData["file_id"], "")
	filePathRemote := anyString(stsData["file_path"], "")
	bucketName := anyString(stsData["bucketname"], "")
	endpoint := strings.TrimPrefix(strings.TrimSpace(anyString(stsData["endpoint"], "")), "https://")
	endpoint = strings.TrimPrefix(endpoint, "http://")
	region := normalizeSignRegion(anyString(stsData["region"], ""))
	accessKeyID := anyString(stsData["access_key_id"], "")
	accessKeySecret := anyString(stsData["access_key_secret"], "")
	securityToken := anyString(stsData["security_token"], "")
	if fileID == "" || filePathRemote == "" || bucketName == "" || endpoint == "" || accessKeyID == "" || accessKeySecret == "" {
		return nil, fmt.Errorf("getstsToken missing required fields: %s", truncate(text, 200))
	}

	clientOptions := []oss.ClientOption{
		oss.SecurityToken(securityToken),
	}
	if region != "" {
		clientOptions = append(clientOptions, oss.Region(region))
	}
	clientOptions = append(clientOptions, oss.AuthVersion(oss.AuthV4))
	ossClient, err := oss.New("https://"+endpoint, accessKeyID, accessKeySecret, clientOptions...)
	if err != nil {
		return nil, err
	}
	bucket, err := ossClient.Bucket(bucketName)
	if err != nil {
		return nil, err
	}
	if err := bucket.PutObject(filePathRemote, bytes.NewReader(raw), oss.ContentType(contentType)); err != nil {
		return nil, err
	}

	status, text, err = app.client.requestJSON(ctx, http.MethodPost, "/api/v2/files/parse", qwenCreds(acc.Token, acc.Cookies), map[string]any{"file_id": fileID}, 20*time.Second)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("files/parse failed: %d %s", status, truncate(text, 200))
	}

	deadline := time.Now().Add(time.Duration(maxInt(app.settings.ContextUploadParseTimeoutSeconds, 1)) * time.Second)
	parseStatus := "pending"
	for time.Now().Before(deadline) {
		status, text, err = app.client.requestJSON(ctx, http.MethodPost, "/api/v2/files/parse/status", qwenCreds(acc.Token, acc.Cookies), map[string]any{"file_id_list": []string{fileID}}, 20*time.Second)
		if err != nil {
			return nil, err
		}
		if status != http.StatusOK {
			return nil, fmt.Errorf("files/parse/status failed: %d %s", status, truncate(text, 200))
		}
		var payload map[string]any
		if err := json.Unmarshal([]byte(text), &payload); err != nil {
			return nil, err
		}
		rows := anyList(payload["data"])
		row := map[string]any{}
		if len(rows) > 0 {
			row, _ = rows[0].(map[string]any)
		}
		parseStatus = anyString(row["status"], "pending")
		if parseStatus == "success" {
			break
		}
		if parseStatus == "failed" || parseStatus == "error" {
			return nil, fmt.Errorf("file parse failed: %s", truncate(mustJSON(row), 200))
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(defaultContextUploadPoll):
		}
	}
	if parseStatus != "success" {
		return nil, fmt.Errorf("file parse timeout: %s", fileID)
	}

	nowMillis := time.Now().UnixMilli()
	userID := ""
	if parts := strings.SplitN(strings.TrimLeft(filePathRemote, "/"), "/", 2); len(parts) >= 2 {
		userID = parts[0]
	}
	putURL := "https://" + bucketName + "." + endpoint + "/" + strings.TrimLeft(filePathRemote, "/")
	remoteRef := map[string]any{
		"type": "file",
		"file": map[string]any{
			"created_at": nowMillis,
			"data":       map[string]any{},
			"filename":   local.Filename,
			"hash":       nil,
			"id":         fileID,
			"user_id":    userID,
			"meta": map[string]any{
				"name":         local.Filename,
				"size":         len(raw),
				"content_type": contentType,
				"parse_meta":   map[string]any{"parse_status": parseStatus},
			},
			"update_at": nowMillis,
		},
		"id":              fileID,
		"url":             putURL,
		"name":            local.Filename,
		"collection_name": "",
		"progress":        0,
		"status":          "uploaded",
		"greenNet":        "success",
		"size":            len(raw),
		"error":           "",
		"itemId":          randomID(),
		"file_type":       contentType,
		"showType":        "file",
		"file_class":      upstreamFileClass(contentType),
		"uploadTaskId":    randomID(),
	}
	return map[string]any{
		"remote_file_id":    fileID,
		"remote_object_key": filePathRemote,
		"filename":          local.Filename,
		"content_type":      contentType,
		"parse_status":      parseStatus,
		"remote_ref":        remoteRef,
	}, nil
}

func uniqueRemoteRefs(base []map[string]any, more ...map[string]any) []map[string]any {
	combined := append([]map[string]any{}, base...)
	combined = append(combined, more...)
	return dedupeUpstreamFiles(combined)
}

func extensionLower(name string) string {
	return strings.TrimPrefix(strings.ToLower(filepath.Ext(name)), ".")
}

func (app *App) prepareContextAttachments(ctx context.Context, payload map[string]any, surface, authToken, clientProfile string, tools []map[string]any, attachments []NormalizedAttachment) (PreparedRequestContext, error) {
	workspaceRoot := deriveWorkspaceRoot(payload)
	sessionKey := deriveSessionKey(surface, authToken, payload)
	record, allRecords, err := app.loadSessionRecord(sessionKey)
	if err != nil {
		return PreparedRequestContext{}, err
	}
	plan := planContextOffload(app.settings, anyList(payload["messages"]), tools, clientProfile)
	useGeneratedContextFiles := len(plan.GeneratedFiles) > 0 && len(tools) == 0
	upstreamFiles := []map[string]any{}
	for _, raw := range anyList(payload["upstream_files"]) {
		if item, ok := raw.(map[string]any); ok {
			upstreamFiles = append(upstreamFiles, item)
		}
	}
	if record != nil {
		upstreamFiles = append(upstreamFiles, record.UploadedFiles...)
	}
	upstreamFiles = dedupeUpstreamFiles(upstreamFiles)
	preferredEmail := ""
	if record != nil {
		preferredEmail = record.AccountEmail
	}
	if len(attachments) == 0 && !useGeneratedContextFiles {
		return PreparedRequestContext{
			Payload:           payload,
			SessionKey:        sessionKey,
			ContextMode:       "inline",
			UpstreamFiles:     upstreamFiles,
			BoundAccountEmail: preferredEmail,
			WorkspaceRoot:     workspaceRoot,
		}, nil
	}

	acc, err := app.accounts.Acquire(ctx, preferredEmail)
	if err != nil {
		return PreparedRequestContext{}, err
	}
	boundRecord := SessionAffinityRecord{
		SessionKey:    sessionKey,
		Surface:       surface,
		AccountEmail:  acc.Email,
		UploadedFiles: upstreamFiles,
		ChatID:        "",
		MessageHashes: nil,
		UpdatedAt:     time.Now().Unix(),
		ExpiresAt:     time.Now().Add(time.Duration(maxInt(app.settings.ContextAttachmentTTLSeconds, minSessionAffinityTTL)) * time.Second).Unix(),
	}
	if record != nil {
		if record.ChatID != "" {
			boundRecord.ChatID = record.ChatID
		}
		if len(record.MessageHashes) > 0 {
			boundRecord.MessageHashes = append([]string(nil), record.MessageHashes...)
		}
	}
	allRecords = upsertSessionRecord(allRecords, boundRecord)
	if err := app.saveSessionAffinityRecords(allRecords); err != nil {
		app.accounts.Release(acc)
		return PreparedRequestContext{}, err
	}

	cacheEntries, err := app.loadContextCacheEntries()
	if err != nil {
		app.accounts.Release(acc)
		return PreparedRequestContext{}, err
	}
	cacheChanged := false
	localCleanup := []UploadedLocalFileRecord{}
	appendRemote := func(remote map[string]any) {
		if remote == nil {
			return
		}
		if ref, ok := remote["remote_ref"].(map[string]any); ok {
			upstreamFiles = append(upstreamFiles, ref)
			boundRecord.UploadedFiles = append(boundRecord.UploadedFiles, ref)
		}
	}

	uploadOne := func(local UploadedLocalFileRecord, filename string) error {
		ext := extensionLower(filename)
		if ext == "" {
			ext = extensionLower(local.Filename)
		}
		if cached := findUpstreamCacheEntry(cacheEntries, sessionKey, acc.Email, local.SHA256, ext); cached != nil {
			appendRemote(cached.RemoteFileMeta)
			return nil
		}
		remote, err := app.uploadLocalFileToUpstream(ctx, acc, local)
		if err != nil {
			return err
		}
		appendRemote(remote)
		cacheEntries = append(cacheEntries, UpstreamFileCacheEntry{
			SessionKey:     sessionKey,
			AccountEmail:   acc.Email,
			SHA256:         local.SHA256,
			Ext:            ext,
			Filename:       local.Filename,
			RemoteFileMeta: remote,
			CreatedAt:      time.Now().Unix(),
			ExpiresAt:      time.Now().Add(time.Duration(maxInt(app.settings.ContextAttachmentTTLSeconds, minSessionAffinityTTL)) * time.Second).Unix(),
		})
		cacheChanged = true
		return nil
	}

	cleanupAndRelease := func() {
		for _, local := range localCleanup {
			if local.Ephemeral {
				_ = safeRemoveGeneratedPath(app.settings.ContextGeneratedDir, local.Path)
				_ = app.deleteUploadedLocalFileRecord(local.ID)
			}
		}
	}

	for _, attachment := range attachments {
		if attachment.RemoteRef != nil {
			upstreamFiles = append(upstreamFiles, attachment.RemoteRef)
			continue
		}
		if strings.TrimSpace(attachment.LocalPath) == "" {
			continue
		}
		local := UploadedLocalFileRecord{
			ID:          attachment.FileID,
			Filename:    attachment.Filename,
			ContentType: attachment.ContentType,
			Path:        attachment.LocalPath,
			SHA256:      attachment.SHA256,
			CreatedAt:   time.Now().Unix(),
			Source:      attachment.Source,
			Purpose:     attachment.Purpose,
			Ephemeral:   strings.HasPrefix(strings.ToLower(attachment.Source), "inline"),
		}
		if err := uploadOne(local, attachment.Filename); err != nil {
			app.accounts.Release(acc)
			cleanupAndRelease()
			fallback := deepCopyMap(payload)
			names := []string{}
			for _, item := range attachments {
				if item.Filename != "" {
					names = append(names, item.Filename)
				}
			}
			summary := "Attachment upload failed. Continue with the available inline context only."
			if len(names) > 0 {
				summary = "Attachment upload failed. Attachment names: " + strings.Join(names, ", ")
			}
			fallback["messages"] = []any{map[string]any{"role": "user", "content": summary + "\n\n" + systemContextPromptNote}}
			return PreparedRequestContext{
				Payload:            fallback,
				SessionKey:         sessionKey,
				ContextMode:        "inline",
				UpstreamFiles:      dedupeUpstreamFiles(upstreamFiles),
				WorkspaceRoot:      workspaceRoot,
				AttachmentFallback: true,
				BoundAccountEmail:  "",
			}, nil
		}
		localCleanup = append(localCleanup, local)
	}

	for _, generated := range plan.GeneratedFiles {
		if !useGeneratedContextFiles {
			break
		}
		local, err := app.saveLocalBytes(generated.Filename, generated.ContentType, []byte(generated.Text), "context-file", "context", authToken, true)
		if err != nil {
			app.accounts.Release(acc)
			cleanupAndRelease()
			return PreparedRequestContext{}, err
		}
		if err := uploadOne(local, generated.Filename); err != nil {
			app.accounts.Release(acc)
			cleanupAndRelease()
			return PreparedRequestContext{}, err
		}
		localCleanup = append(localCleanup, local)
	}

	if cacheChanged {
		sort.Slice(cacheEntries, func(i, j int) bool {
			if cacheEntries[i].ExpiresAt == cacheEntries[j].ExpiresAt {
				return cacheEntries[i].CreatedAt < cacheEntries[j].CreatedAt
			}
			return cacheEntries[i].ExpiresAt < cacheEntries[j].ExpiresAt
		})
		if err := app.saveContextCacheEntries(cacheEntries); err != nil {
			app.accounts.Release(acc)
			cleanupAndRelease()
			return PreparedRequestContext{}, err
		}
	}

	boundRecord.UploadedFiles = dedupeUpstreamFiles(boundRecord.UploadedFiles)
	allRecords = upsertSessionRecord(allRecords, boundRecord)
	if err := app.saveSessionAffinityRecords(allRecords); err != nil {
		app.accounts.Release(acc)
		cleanupAndRelease()
		return PreparedRequestContext{}, err
	}
	rewritten := deepCopyMap(payload)
	if useGeneratedContextFiles {
		rewritten["messages"] = plan.InlineMessages
	}
	rewritten["upstream_files"] = dedupeUpstreamFiles(upstreamFiles)
	cleanupAndRelease()
	return PreparedRequestContext{
		Payload:            rewritten,
		SessionKey:         sessionKey,
		ContextMode:        firstNonEmpty(plan.Mode, "inline"),
		UpstreamFiles:      dedupeUpstreamFiles(upstreamFiles),
		BoundAccount:       acc,
		BoundAccountEmail:  acc.Email,
		WorkspaceRoot:      workspaceRoot,
		AttachmentFallback: false,
	}, nil
}

func safeRemoveGeneratedPath(rootDir, target string) error {
	rootDir = normalizeWorkspacePath(rootDir)
	target = normalizeWorkspacePath(target)
	if rootDir == "" || target == "" {
		return nil
	}
	if !strings.HasPrefix(strings.ToLower(target), strings.ToLower(rootDir)+strings.ToLower(string(filepath.Separator))) && !strings.EqualFold(target, rootDir) {
		return nil
	}
	if info, err := os.Stat(target); err == nil && !info.IsDir() {
		return os.Remove(target)
	}
	return nil
}

func (app *App) rewriteCachedFileHints(payload map[string]any, authToken string) map[string]any {
	if app == nil || app.fileContentCache == nil || strings.TrimSpace(authToken) == "" {
		return payload
	}
	rewritten := deepCopyMap(payload)
	refs := collectToolCallRefs(anyList(rewritten["messages"]))
	messages := anyList(rewritten["messages"])
	changed := false
	for idx, raw := range messages {
		msg, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		role := stringValue(msg, "role", "")
		if role == "tool" {
			ref := refs[stringValue(msg, "tool_call_id", "")]
			if !isReadLikeToolName(ref.Name) || strings.TrimSpace(ref.FilePath) == "" {
				continue
			}
			text := flattenContentText(msg["content"])
			if isFileCacheHint(text) {
				if cached, ok := app.fileContentCache.Get(authToken, ref.FilePath); ok {
					msg["content"] = cached
					changed = true
				}
			} else {
				app.fileContentCache.Put(authToken, ref.FilePath, text)
			}
			messages[idx] = msg
			continue
		}
		contentList, ok := msg["content"].([]any)
		if !ok {
			continue
		}
		partChanged := false
		for partIdx, rawPart := range contentList {
			part, ok := rawPart.(map[string]any)
			if !ok {
				continue
			}
			partType := stringValue(part, "type", "")
			if partType != "tool_result" && partType != "function_call_output" {
				continue
			}
			ref := refs[firstNonEmpty(anyString(part["tool_use_id"], ""), anyString(part["call_id"], ""), anyString(part["id"], ""))]
			if !isReadLikeToolName(ref.Name) || strings.TrimSpace(ref.FilePath) == "" {
				continue
			}
			text := flattenContentText(part["content"])
			if isFileCacheHint(text) {
				if cached, ok := app.fileContentCache.Get(authToken, ref.FilePath); ok {
					part["content"] = cached
					contentList[partIdx] = part
					partChanged = true
					changed = true
				}
			} else {
				app.fileContentCache.Put(authToken, ref.FilePath, text)
			}
		}
		if partChanged {
			msg["content"] = contentList
			messages[idx] = msg
		}
	}
	if changed {
		rewritten["messages"] = messages
	}
	return rewritten
}

func collectToolCallRefs(messages []any) map[string]toolCallRef {
	refs := map[string]toolCallRef{}
	for _, raw := range messages {
		msg, ok := raw.(map[string]any)
		if !ok || stringValue(msg, "role", "") != "assistant" {
			continue
		}
		for _, rawCall := range anyList(msg["tool_calls"]) {
			call, ok := rawCall.(map[string]any)
			if !ok {
				continue
			}
			callID := anyString(call["id"], "")
			fn, _ := call["function"].(map[string]any)
			name := anyString(fn["name"], "")
			filePath := extractToolFilePathFromAny(fn["arguments"])
			if callID != "" {
				refs[callID] = toolCallRef{Name: name, FilePath: filePath}
			}
		}
		for _, rawPart := range anyList(msg["content"]) {
			part, ok := rawPart.(map[string]any)
			if !ok || stringValue(part, "type", "") != "tool_use" {
				continue
			}
			callID := firstNonEmpty(anyString(part["id"], ""), anyString(part["tool_use_id"], ""))
			name := anyString(part["name"], "")
			filePath := extractToolFilePathFromAny(part["input"])
			if callID != "" {
				refs[callID] = toolCallRef{Name: name, FilePath: filePath}
			}
		}
	}
	return refs
}

func extractToolFilePathFromAny(value any) string {
	switch v := value.(type) {
	case nil:
		return ""
	case string:
		trimmed := strings.TrimSpace(v)
		if trimmed == "" {
			return ""
		}
		var parsed any
		if json.Unmarshal([]byte(trimmed), &parsed) == nil {
			return extractToolFilePathFromAny(parsed)
		}
		return trimmed
	case map[string]any:
		for _, key := range []string{"file_path", "path", "filepath", "filename"} {
			if path := strings.TrimSpace(anyString(v[key], "")); path != "" {
				return path
			}
		}
	case []any:
		for _, item := range v {
			if path := extractToolFilePathFromAny(item); path != "" {
				return path
			}
		}
	}
	return ""
}

func isReadLikeToolName(name string) bool {
	key := strings.ToLower(regexp.MustCompile(`[^a-z0-9]+`).ReplaceAllString(name, ""))
	switch key {
	case "read", "readfile", "fsopenfile", "readx", "openfile":
		return true
	default:
		return false
	}
}

func (app *App) prepareStandardRequest(ctx context.Context, r *http.Request, body map[string]any, defaultModel, surface, authToken string) (StandardRequest, error) {
	preprocessed, err := app.preprocessAttachments(body, authToken)
	if err != nil {
		return StandardRequest{}, err
	}
	payload := app.rewriteCachedFileHints(preprocessed.Payload, authToken)
	workspaceRoot := deriveWorkspaceRoot(payload)
	payload = injectWorkspaceNotice(payload, workspaceRoot)
	req := buildChatStandardRequest(payload, defaultModel, surface)
	clientProfile := detectClientProfile(r, req.Tools)
	prepared, err := app.prepareContextAttachments(ctx, payload, surface, authToken, clientProfile, req.Tools, preprocessed.Attachments)
	if err != nil {
		return StandardRequest{}, err
	}
	finalPayload := injectWorkspaceNotice(prepared.Payload, prepared.WorkspaceRoot)
	finalPayload = app.rewriteCachedFileHints(finalPayload, authToken)
	req = buildChatStandardRequest(finalPayload, defaultModel, surface)
	req.SessionKey = prepared.SessionKey
	req.WorkspaceRoot = prepared.WorkspaceRoot
	req.ClientProfile = clientProfile
	req.ContextMode = prepared.ContextMode
	req.UpstreamFiles = prepared.UpstreamFiles
	req.BoundAccount = prepared.BoundAccount
	req.PreferredEmail = prepared.BoundAccountEmail
	return req, nil
}

func (app *App) cleanupContextArtifacts(ctx context.Context) {
	now := time.Now().Unix()
	if records, err := app.loadUploadedLocalFiles(); err == nil {
		next := records[:0]
		changed := false
		for _, record := range records {
			expired := record.Ephemeral && record.CreatedAt > 0 && now-record.CreatedAt > int64(maxInt(app.settings.ContextAttachmentTTLSeconds, minSessionAffinityTTL))
			if expired {
				_ = safeRemoveGeneratedPath(app.settings.ContextGeneratedDir, record.Path)
				changed = true
				continue
			}
			next = append(next, record)
		}
		if changed {
			_ = app.saveUploadedLocalFiles(next)
		}
	}
	if records, err := app.loadContextCacheEntries(); err == nil {
		next := records[:0]
		changed := false
		for _, record := range records {
			if record.ExpiresAt > 0 && record.ExpiresAt < now {
				changed = true
				continue
			}
			next = append(next, record)
		}
		if changed {
			_ = app.saveContextCacheEntries(next)
		}
	}
	if records, err := app.loadSessionAffinityRecords(); err == nil {
		next := records[:0]
		changed := false
		for _, record := range records {
			if record.ExpiresAt > 0 && record.ExpiresAt < now {
				changed = true
				continue
			}
			next = append(next, record)
		}
		if changed {
			_ = app.saveSessionAffinityRecords(next)
		}
	}
	if app != nil {
		app.logInfo(ctx, "上下文缓存清理完成")
	}
}
