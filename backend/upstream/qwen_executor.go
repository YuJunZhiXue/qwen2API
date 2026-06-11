package upstream

import (
	"context"
	"encoding/json"
	"strings"
)

// ChatClient is the narrow upstream contract required to execute one Qwen turn.
type ChatClient interface {
	CreateChat(ctx context.Context, token, cookies, model, chatType string) (string, error)
	DeleteChat(ctx context.Context, token, cookies, chatID string) bool
	StreamChat(ctx context.Context, token, cookies, chatID string, payload map[string]any, onEvent func(Event) error) error
}

type ExecuteOptions struct {
	Token           string
	Cookies         string
	Model           string
	Prompt          string
	ChatType        string
	HasCustomTools  bool
	Files           []map[string]any
	MediaOptions    map[string]any
	ThinkingEnabled *bool
	EnableSearch    bool
	DeleteWhenDone  bool
}

// ExecuteTurn creates a chat, streams one payload, and optionally deletes the
// temporary upstream conversation.
func ExecuteTurn(ctx context.Context, client ChatClient, opts ExecuteOptions, onEvent func(Event) error) (string, error) {
	chatType := NormalizeChatType(opts.ChatType)
	chatID, err := client.CreateChat(ctx, opts.Token, opts.Cookies, opts.Model, chatType)
	if err != nil {
		return "", err
	}
	if opts.DeleteWhenDone {
		defer client.DeleteChat(context.Background(), opts.Token, opts.Cookies, chatID)
	}
	payload := BuildChatPayload(chatID, opts.Model, opts.Prompt, opts.HasCustomTools, opts.Files, chatType, opts.MediaOptions, opts.ThinkingEnabled, opts.EnableSearch)
	if err := client.StreamChat(ctx, opts.Token, opts.Cookies, chatID, payload, onEvent); err != nil {
		return chatID, err
	}
	return chatID, nil
}

func FormatUpstreamError(obj map[string]any) string {
	if obj == nil {
		return ""
	}
	requestID := firstString(obj["request_id"], obj["response_id"])
	if requestID == "" {
		requestID = "-"
	}
	if success, ok := obj["success"].(bool); ok && !success {
		data, _ := obj["data"].(map[string]any)
		code := firstString(data["code"], obj["code"])
		if code == "" {
			code = "upstream_error"
		}
		details := firstString(data["details"], data["message"], obj["details"], obj["message"])
		return "Qwen upstream error code=" + code + " request_id=" + requestID + " details=" + details
	}
	if errorObj, ok := obj["error"].(map[string]any); ok {
		code := firstString(errorObj["code"])
		if code == "" {
			code = "upstream_error"
		}
		details := firstString(errorObj["details"], errorObj["message"], errorObj["type"])
		return "Qwen upstream error code=" + code + " request_id=" + requestID + " details=" + details
	}
	if errorText, ok := obj["error"].(string); ok && strings.TrimSpace(errorText) != "" {
		return "Qwen upstream error request_id=" + requestID + " details=" + errorText
	}
	return ""
}

func ExtractUpstreamError(text string) string {
	for _, rawLine := range strings.Split(text, "\n") {
		line := strings.TrimSpace(rawLine)
		if line == "" {
			continue
		}
		if strings.HasPrefix(line, "data:") {
			line = strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		}
		if line == "" || line == "[DONE]" || !strings.HasPrefix(line, "{") {
			continue
		}
		var obj map[string]any
		if json.Unmarshal([]byte(line), &obj) != nil {
			continue
		}
		if message := FormatUpstreamError(obj); message != "" {
			return message
		}
	}
	return ""
}
