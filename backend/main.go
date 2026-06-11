package main

// Go runtime entrypoint for the migrated backend.

import (
	"bufio"
	"bytes"
	"context"
	cryptorand "crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	pw "github.com/playwright-community/playwright-go"
	"hash/fnv"
	"html"
	"io"
	"log/slog"
	"math"
	mathrand "math/rand"
	"mime"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"runtime/debug"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"qwen2api-go/adapter"
	apidesc "qwen2api-go/api"
	"qwen2api-go/core"
	rt "qwen2api-go/runtime"
	"qwen2api-go/services"
	"qwen2api-go/toolcall"
	"qwen2api-go/upstream"
)

// ---- migrated from main.go ----
type App struct {
	settings       Settings
	logger         *slog.Logger
	accounts       *AccountPool
	client         *QwenClient
	chatPool       *ChatIDPool
	apiKeys        map[string]bool
	managedAPIKeys map[string]bool
	envAPIKeys     map[string]bool

	usersStore        *JSONStore
	accountsStore     *JSONStore
	capturesStore     *JSONStore
	configStore       *JSONStore
	contextCacheStore *JSONStore
	uploadedFileStore *JSONStore
	sessionStore      *JSONStore
	fileContentCache  *fileContentCache
	keepalive         *KeepAliveService
}

func main() {
	settings := LoadSettings()
	logger := newLogger(settings.LogLevel)
	if strings.TrimSpace(settings.AdminKey) == "" {
		logger.Warn("ADMIN_KEY is not set; configure it before using WebUI or admin APIs")
	}
	runMigratedPackageSelfCheck(logger)
	if len(os.Args) > 1 && os.Args[1] == "--install-browsers" {
		if err := installPlaywrightBrowsers(logger); err != nil {
			logger.Error("failed to install Playwright browsers", "error", err)
			os.Exit(1)
		}
		logger.Info("Playwright browsers installed")
		return
	}

	app, err := NewApp(settings, logger)
	if err != nil {
		logger.Error("failed to initialize app", "error", err)
		os.Exit(1)
	}
	backgroundCtx, stopBackground := context.WithCancel(context.Background())
	defer stopBackground()

	addr := fmt.Sprintf(":%d", settings.Port)
	srv := &http.Server{
		Addr:              addr,
		Handler:           app.routes(),
		ReadHeaderTimeout: 30 * time.Second,
		ReadTimeout:       0,
		WriteTimeout:      0,
		IdleTimeout:       120 * time.Second,
	}

	ln, err := net.Listen("tcp", addr)
	if err != nil {
		logger.Error("server listen failed", "port", settings.Port, "error", err)
		os.Exit(1)
	}

	errCh := make(chan error, 1)
	go func() {
		logger.Info("qwen2API Go backend starting", "port", settings.Port, "version", Version)
		errCh <- srv.Serve(ln)
	}()
	app.StartBackground(backgroundCtx)

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)

	select {
	case sig := <-stop:
		logger.Info("shutdown requested", "signal", sig.String())
		stopBackground()
	case err := <-errCh:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("server failed", "error", err)
			os.Exit(1)
		}
		stopBackground()
	}

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		logger.Error("graceful shutdown failed", "error", err)
		os.Exit(1)
	}
	logger.Info("qwen2API Go backend stopped")
}

func NewApp(settings Settings, logger *slog.Logger) (*App, error) {
	if err := os.MkdirAll(settings.DataDir, 0o755); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(settings.LogsDir, 0o755); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(settings.ContextGeneratedDir, 0o755); err != nil {
		return nil, err
	}

	app := &App{
		settings:          settings,
		logger:            logger,
		usersStore:        NewJSONStore(settings.UsersFile, []any{}),
		accountsStore:     NewJSONStore(settings.AccountsFile, []any{}),
		capturesStore:     NewJSONStore(settings.CapturesFile, []any{}),
		configStore:       NewJSONStore(settings.ConfigFile, map[string]any{}),
		contextCacheStore: NewJSONStore(settings.ContextCacheFile, []any{}),
		uploadedFileStore: NewJSONStore(settings.UploadedFilesFile, []any{}),
		sessionStore:      NewJSONStore(settings.ContextAffinityFile, []any{}),
		fileContentCache:  newFileContentCache(),
	}

	app.apiKeys, app.managedAPIKeys, app.envAPIKeys = loadAPIKeys(settings.APIKeysFile, logger)
	app.accounts = NewAccountPool(app.accountsStore, settings, logger)
	if err := app.accounts.Load(); err != nil {
		return nil, err
	}
	app.client = NewQwenClient(app.accounts, settings, logger)
	app.chatPool = NewChatIDPool(app.client, app.accounts, settings, logger)
	app.keepalive = NewKeepAliveService(logger)
	return app, nil
}

func (app *App) StartBackground(ctx context.Context) {
	if app == nil {
		return
	}
	app.chatPool.Start(ctx)
	if app.keepalive != nil {
		app.keepalive.Start(ctx, app.keepaliveConfig())
	}
	go func() {
		ticker := time.NewTicker(time.Minute)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				app.cleanupContextArtifacts(ctx)
			}
		}
	}()
}

func newLogger(levelText string) *slog.Logger {
	level := slog.LevelInfo
	switch normalizeLower(levelText) {
	case "debug":
		level = slog.LevelDebug
	case "warn", "warning":
		level = slog.LevelWarn
	case "error":
		level = slog.LevelError
	}
	return slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: level}))
}

func repoRootFromCwd() string {
	wd, err := os.Getwd()
	if err != nil {
		return "."
	}
	cleaned := filepath.Clean(wd)
	if strings.EqualFold(filepath.Base(cleaned), "backend") {
		parent := filepath.Dir(cleaned)
		if _, err := os.Stat(filepath.Join(parent, "frontend")); err == nil {
			return parent
		}
	}
	return cleaned
}

func runMigratedPackageSelfCheck(logger *slog.Logger) {
	routes := 0
	routes += len(apidesc.ProbeRoutes())
	routes += len(apidesc.AdminRoutes())
	routes += len(apidesc.ModelRoutes())
	routes += len(apidesc.ChatRoutes())
	routes += len(apidesc.ResponsesRoutes())
	routes += len(apidesc.AnthropicRoutes())
	routes += len(apidesc.GeminiRoutes())
	routes += len(apidesc.ImageRoutes())
	routes += len(apidesc.VideoRoutes())
	routes += len(apidesc.FileRoutes())
	routes += len(apidesc.EmbeddingRoutes())

	coreChecks := []any{
		core.Version,
		core.RedactToken("abcdefghijklmnopqrstuvwxyz"),
		core.ChooseEngine(false, true),
		core.DefaultBrowserOptions(core.LoadSettings(repoRootFromCwd())).PoolSize,
		rt.VisibleText("visible<think>hidden</think>"),
	}
	if logger != nil {
		logger.Debug("migrated Go package self-check", "routes", routes, "core_checks", len(coreChecks))
	}
}

// ---- migrated from accounts.go ----
type Account struct {
	Email               string  `json:"email"`
	Password            string  `json:"password"`
	Token               string  `json:"token"`
	Cookies             string  `json:"cookies"`
	Username            string  `json:"username"`
	Source              string  `json:"source,omitempty"`
	EnvName             string  `json:"env_name,omitempty"`
	ActivationPending   bool    `json:"activation_pending"`
	StatusCode          string  `json:"status_code"`
	LastError           string  `json:"last_error"`
	LastRequestStarted  float64 `json:"last_request_started"`
	LastRequestFinished float64 `json:"last_request_finished"`
	ConsecutiveFailures int     `json:"consecutive_failures"`
	RateLimitStrikes    int     `json:"rate_limit_strikes"`

	Valid            bool                        `json:"valid,omitempty"`
	Inflight         int                         `json:"inflight,omitempty"`
	RateLimitedUntil float64                     `json:"rate_limited_until,omitempty"`
	RateLimits       map[string]AccountRateLimit `json:"rate_limits,omitempty"`
}

type AccountRateLimit struct {
	Until     float64 `json:"until,omitempty"`
	Reason    string  `json:"reason,omitempty"`
	LastError string  `json:"last_error,omitempty"`
	Strikes   int     `json:"strikes,omitempty"`
}

type AccountPool struct {
	store    *JSONStore
	settings Settings
	logger   *slog.Logger

	mu                     sync.Mutex
	accounts               []*Account
	maxInflightPerAccount  int
	globalInUse            int
	globalMaxInflight      int
	recommendedConcurrency int
	maxQueueSize           int
	readySetEnabled        bool
}

const (
	accountUsageChat     = "chat"
	accountUsageImage    = "image"
	accountUsageVideo    = "video"
	accountUsageMetadata = "metadata"
	accountUsageUnknown  = "unknown"
)

func NewAccountPool(store *JSONStore, settings Settings, logger *slog.Logger) *AccountPool {
	return &AccountPool{
		store:                 store,
		settings:              settings,
		logger:                logger,
		maxInflightPerAccount: maxInt(settings.MaxInflightPerAccount, 1),
	}
}

func (p *AccountPool) Load() error {
	if err := p.store.Ensure(); err != nil {
		return err
	}
	var data []Account
	if err := p.store.LoadInto(&data); err != nil {
		return err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	p.accounts = make([]*Account, 0, len(data))
	for i := range data {
		data[i].normalize()
		data[i].migrateLegacyRateLimit(p.settings)
		p.accounts = append(p.accounts, &data[i])
	}
	for _, envAcc := range loadEnvAccounts() {
		envAcc.normalize()
		envAcc.migrateLegacyRateLimit(p.settings)
		replaced := false
		for i, existing := range p.accounts {
			if existing.Email == envAcc.Email && envAcc.Email != "" {
				cp := envAcc
				p.accounts[i] = &cp
				replaced = true
				break
			}
		}
		if !replaced {
			cp := envAcc
			p.accounts = append(p.accounts, &cp)
		}
	}
	p.resetLocked()
	p.logger.Info("loaded upstream accounts", "count", len(p.accounts))
	return nil
}

func (p *AccountPool) Save() error {
	p.mu.Lock()
	data := make([]Account, 0, len(p.accounts))
	for _, acc := range p.accounts {
		if acc.Source == "env" {
			continue
		}
		cp := *acc
		cp.Valid = false
		cp.Inflight = 0
		cp.RateLimits = cloneRateLimits(acc.RateLimits)
		cp.syncLegacyRateLimit()
		data = append(data, cp)
	}
	p.mu.Unlock()
	return p.store.Save(data)
}

func (p *AccountPool) resetLocked() {
	valid := 0
	available := 0
	for _, acc := range p.accounts {
		if acc.Valid {
			valid++
		}
		if acc.availableFor(p.settings, accountUsageChat) {
			available++
		}
	}
	p.recommendedConcurrency = available * p.maxInflightPerAccount
	p.globalMaxInflight = p.recommendedConcurrency
	p.maxQueueSize = p.recommendedConcurrency
	p.readySetEnabled = valid >= maxInt(p.settings.AccountReadySetThreshold, 1)
}

func (p *AccountPool) Snapshot() []Account {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]Account, 0, len(p.accounts))
	for _, acc := range p.accounts {
		acc.syncLegacyRateLimit()
		cp := *acc
		cp.RateLimits = cloneRateLimits(acc.RateLimits)
		cp.StatusCode = acc.status()
		out = append(out, cp)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Email < out[j].Email })
	return out
}

func (p *AccountPool) Status() map[string]any {
	p.mu.Lock()
	defer p.mu.Unlock()
	total := len(p.accounts)
	valid := 0
	available := 0
	availableImage := 0
	availableVideo := 0
	for _, acc := range p.accounts {
		if acc.Valid {
			valid++
		}
		if acc.availableFor(p.settings, accountUsageChat) && acc.Inflight < p.maxInflightPerAccount {
			available++
		}
		if acc.availableFor(p.settings, accountUsageImage) && acc.Inflight < p.maxInflightPerAccount {
			availableImage++
		}
		if acc.availableFor(p.settings, accountUsageVideo) && acc.Inflight < p.maxInflightPerAccount {
			availableVideo++
		}
	}
	return map[string]any{
		"total":                    total,
		"valid":                    valid,
		"available":                available,
		"available_chat":           available,
		"available_image":          availableImage,
		"available_video":          availableVideo,
		"max_inflight_per_account": p.maxInflightPerAccount,
		"recommended_concurrency":  p.recommendedConcurrency,
		"global_max_inflight":      p.globalMaxInflight,
		"max_queue_size":           p.maxQueueSize,
		"global_in_use":            p.globalInUse,
		"ready_set_enabled":        p.readySetEnabled,
		"ready_set_threshold":      p.settings.AccountReadySetThreshold,
	}
}

func (p *AccountPool) HasAvailable() bool {
	return p.HasAvailableFor(accountUsageChat)
}

func (p *AccountPool) HasAvailableFor(usage string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.globalMaxInflight > 0 && p.globalInUse >= p.globalMaxInflight {
		return false
	}
	for _, acc := range p.accounts {
		if acc.availableFor(p.settings, usage) && acc.Inflight < p.maxInflightPerAccount {
			return true
		}
	}
	return false
}

func (p *AccountPool) Add(acc Account) error {
	acc.normalize()
	p.mu.Lock()
	replaced := false
	for i, existing := range p.accounts {
		if existing.Email == acc.Email && acc.Email != "" {
			p.accounts[i] = &acc
			replaced = true
			break
		}
	}
	if !replaced {
		p.accounts = append(p.accounts, &acc)
	}
	p.resetLocked()
	p.mu.Unlock()
	return p.Save()
}

func (p *AccountPool) Remove(email string) error {
	p.mu.Lock()
	for _, acc := range p.accounts {
		if acc.Email == email && acc.Source == "env" {
			p.mu.Unlock()
			return errors.New("environment account cannot be deleted from admin panel")
		}
	}
	next := p.accounts[:0]
	for _, acc := range p.accounts {
		if acc.Email != email {
			next = append(next, acc)
		}
	}
	p.accounts = next
	p.resetLocked()
	p.mu.Unlock()
	return p.Save()
}

func (p *AccountPool) SetMaxInflight(value int) {
	p.mu.Lock()
	p.maxInflightPerAccount = maxInt(value, 1)
	p.resetLocked()
	p.mu.Unlock()
}

func (p *AccountPool) SetReadySetThreshold(value int) {
	p.mu.Lock()
	p.settings.AccountReadySetThreshold = maxInt(value, 1)
	p.resetLocked()
	p.mu.Unlock()
}

func (p *AccountPool) SetGlobalMaxInflight(value int) {
	if value <= 0 {
		return
	}
	p.mu.Lock()
	p.globalMaxInflight = value
	p.mu.Unlock()
}

func (p *AccountPool) Acquire(ctx context.Context, preferredEmail string) (*Account, error) {
	return p.AcquireFor(ctx, preferredEmail, accountUsageChat)
}

func (p *AccountPool) AcquireFor(ctx context.Context, preferredEmail, usage string) (*Account, error) {
	deadline := time.Now().Add(60 * time.Second)
	for {
		p.mu.Lock()
		acc := p.pickLockedFor(preferredEmail, usage)
		if acc != nil {
			now := float64(time.Now().UnixNano()) / 1e9
			acc.Inflight++
			acc.LastRequestStarted = now
			p.globalInUse++
			p.mu.Unlock()
			return acc, nil
		}
		p.mu.Unlock()
		if time.Now().After(deadline) {
			return nil, errors.New("no available upstream account")
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(150 * time.Millisecond):
		}
	}
}

func (p *AccountPool) pickLocked(preferredEmail string) *Account {
	return p.pickLockedFor(preferredEmail, accountUsageChat)
}

func (p *AccountPool) pickLockedFor(preferredEmail, usage string) *Account {
	if p.globalMaxInflight > 0 && p.globalInUse >= p.globalMaxInflight {
		return nil
	}
	var candidates []*Account
	for _, acc := range p.accounts {
		if preferredEmail != "" && acc.Email != preferredEmail {
			continue
		}
		if !acc.availableFor(p.settings, usage) || acc.Inflight >= p.maxInflightPerAccount {
			continue
		}
		candidates = append(candidates, acc)
	}
	if preferredEmail != "" && len(candidates) == 0 {
		return p.pickLockedFor("", usage)
	}
	if len(candidates) == 0 {
		return nil
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].Inflight != candidates[j].Inflight {
			return candidates[i].Inflight < candidates[j].Inflight
		}
		return candidates[i].LastRequestStarted < candidates[j].LastRequestStarted
	})
	if p.settings.RequestJitterMaxMS > 0 {
		minDelay := maxInt(p.settings.RequestJitterMinMS, 0)
		maxDelay := maxInt(p.settings.RequestJitterMaxMS, minDelay)
		time.Sleep(time.Duration(minDelay+mathrand.Intn(maxDelay-minDelay+1)) * time.Millisecond)
	}
	return candidates[0]
}

func (p *AccountPool) Release(acc *Account) {
	if acc == nil {
		return
	}
	p.mu.Lock()
	if acc.Inflight > 0 {
		acc.Inflight--
	}
	if p.globalInUse > 0 {
		p.globalInUse--
	}
	acc.LastRequestFinished = float64(time.Now().UnixNano()) / 1e9
	p.mu.Unlock()
}

func (p *AccountPool) MarkSuccess(acc *Account) {
	p.MarkSuccessFor(acc, accountUsageChat)
}

func (p *AccountPool) MarkSuccessFor(acc *Account, usage string) {
	if acc == nil {
		return
	}
	p.mu.Lock()
	acc.Valid = true
	acc.StatusCode = "valid"
	acc.LastError = ""
	acc.ConsecutiveFailures = 0
	acc.clearRateLimitFor(usage)
	acc.syncLegacyRateLimit()
	p.resetLocked()
	p.mu.Unlock()
}

func (p *AccountPool) MarkInvalid(acc *Account, status, message string) {
	if acc == nil {
		return
	}
	p.mu.Lock()
	acc.Valid = false
	acc.StatusCode = status
	acc.LastError = message
	acc.ConsecutiveFailures++
	p.mu.Unlock()
	_ = p.Save()
}

func (p *AccountPool) MarkRateLimited(acc *Account, cooldown int, message string) {
	p.MarkRateLimitedFor(acc, accountUsageChat, cooldown, message)
}

func (p *AccountPool) MarkRateLimitedFor(acc *Account, usage string, cooldown int, message string) {
	if acc == nil {
		return
	}
	if cooldown <= 0 {
		cooldown = p.settings.RateLimitBaseCooldown
	}
	p.mu.Lock()
	acc.RateLimitStrikes++
	acc.LastError = message
	acc.setRateLimitFor(usage, float64(time.Now().Add(time.Duration(cooldown)*time.Second).UnixNano())/1e9, message)
	acc.syncLegacyRateLimit()
	p.resetLocked()
	p.mu.Unlock()
	_ = p.Save()
}

func (p *AccountPool) MarkVerification(email string, result TokenVerifyResult) error {
	p.mu.Lock()
	for _, acc := range p.accounts {
		if acc.Email != email {
			continue
		}
		acc.Valid = result.Valid
		acc.StatusCode = result.StatusCode
		acc.LastError = result.Error
		if result.Valid {
			acc.ActivationPending = false
			acc.ConsecutiveFailures = 0
			acc.clearRateLimitFor(accountUsageChat)
		} else {
			acc.ConsecutiveFailures++
			if result.StatusCode == "rate_limited" {
				acc.setRateLimitFor(accountUsageChat, float64(time.Now().Add(time.Duration(p.settings.RateLimitBaseCooldown)*time.Second).UnixNano())/1e9, result.Error)
			}
		}
		acc.syncLegacyRateLimit()
		break
	}
	p.resetLocked()
	p.mu.Unlock()
	return p.Save()
}

func (a *Account) normalize() {
	if strings.TrimSpace(a.Source) == "" {
		a.Source = "file"
	}
	if a.StatusCode == "" {
		if a.ActivationPending {
			a.StatusCode = "pending_activation"
		} else {
			a.StatusCode = "valid"
		}
	}
	a.Valid = !a.ActivationPending && a.StatusCode != "invalid" && a.StatusCode != "auth_error" && a.StatusCode != "banned"
	a.compactRateLimits()
}

func (a *Account) available(settings Settings) bool {
	return a.availableFor(settings, accountUsageChat)
}

func (a *Account) availableFor(settings Settings, usage string) bool {
	if a == nil || !a.Valid || a.Token == "" {
		return false
	}
	now := float64(time.Now().UnixNano()) / 1e9
	if a.rateLimitedUntilFor(usage) > now {
		return false
	}
	minInterval := float64(maxInt(settings.AccountMinIntervalMS, 0)) / 1000.0
	return a.LastRequestStarted+minInterval <= now
}

func (a *Account) status() string {
	if a.ActivationPending {
		return "pending_activation"
	}
	if a.rateLimitedUntilFor(accountUsageChat) > float64(time.Now().UnixNano())/1e9 {
		return "rate_limited"
	}
	if a.Valid {
		return "valid"
	}
	if a.StatusCode != "" {
		return a.StatusCode
	}
	return "invalid"
}

func (a *Account) migrateLegacyRateLimit(settings Settings) {
	if a == nil {
		return
	}
	now := float64(time.Now().UnixNano()) / 1e9
	until := a.RateLimitedUntil
	if until <= now && isRateLimitErrorMessage(a.LastError) {
		cooldown := rateLimitCooldownSeconds(settings, a.LastError)
		until = float64(time.Now().Add(time.Duration(cooldown)*time.Second).UnixNano()) / 1e9
	}
	if until > now {
		usage := inferRateLimitUsage(a.LastError)
		if usage != accountUsageUnknown {
			a.setRateLimitFor(usage, until, a.LastError)
		}
	}
	a.syncLegacyRateLimit()
	a.compactRateLimits()
}

func (a *Account) rateLimitedUntilFor(usage string) float64 {
	if a == nil {
		return 0
	}
	state, ok := a.RateLimits[normalizeAccountUsage(usage)]
	if !ok {
		return 0
	}
	return state.Until
}

func (a *Account) setRateLimitFor(usage string, until float64, message string) {
	if a == nil || until <= 0 {
		return
	}
	usage = normalizeAccountUsage(usage)
	if a.RateLimits == nil {
		a.RateLimits = map[string]AccountRateLimit{}
	}
	state := a.RateLimits[usage]
	state.Until = until
	state.Reason = rateLimitReasonForUsage(usage)
	state.LastError = message
	state.Strikes++
	if state.Strikes < a.RateLimitStrikes {
		state.Strikes = a.RateLimitStrikes
	}
	a.RateLimits[usage] = state
}

func (a *Account) clearRateLimitFor(usage string) {
	if a == nil || a.RateLimits == nil {
		return
	}
	delete(a.RateLimits, normalizeAccountUsage(usage))
	if len(a.RateLimits) == 0 {
		a.RateLimits = nil
	}
}

func (a *Account) syncLegacyRateLimit() {
	if a == nil {
		return
	}
	a.RateLimitedUntil = a.rateLimitedUntilFor(accountUsageChat)
}

func (a *Account) compactRateLimits() {
	if a == nil || len(a.RateLimits) == 0 {
		return
	}
	now := float64(time.Now().UnixNano()) / 1e9
	for usage, state := range a.RateLimits {
		normalized := normalizeAccountUsage(usage)
		if normalized != usage {
			delete(a.RateLimits, usage)
		}
		if normalized == accountUsageUnknown && state.Reason == "legacy_unknown_quota_limited" {
			delete(a.RateLimits, usage)
			continue
		}
		if state.Until <= 0 && state.LastError == "" && state.Reason == "" {
			continue
		}
		if state.Until > 0 && state.Until <= now {
			delete(a.RateLimits, usage)
			continue
		}
		state.Reason = firstNonEmpty(state.Reason, rateLimitReasonForUsage(normalized))
		a.RateLimits[normalized] = state
	}
	if len(a.RateLimits) == 0 {
		a.RateLimits = nil
	}
}

func cloneRateLimits(in map[string]AccountRateLimit) map[string]AccountRateLimit {
	if len(in) == 0 {
		return nil
	}
	out := make(map[string]AccountRateLimit, len(in))
	for usage, state := range in {
		out[usage] = state
	}
	return out
}

func normalizeAccountUsage(usage string) string {
	switch strings.ToLower(strings.TrimSpace(usage)) {
	case "", accountUsageChat, "completion", "conversation", "message", "messages", "text", "t2t":
		return accountUsageChat
	case accountUsageImage, "images", "image_gen", "t2i", "picture", "photo":
		return accountUsageImage
	case accountUsageVideo, "videos", "t2v", "video_gen":
		return accountUsageVideo
	case accountUsageMetadata, "models", "model", "account", "verify", "verification":
		return accountUsageMetadata
	case accountUsageUnknown, "legacy", "global":
		return accountUsageUnknown
	default:
		return strings.ToLower(strings.TrimSpace(usage))
	}
}

func rateLimitReasonForUsage(usage string) string {
	switch normalizeAccountUsage(usage) {
	case accountUsageImage:
		return "image_quota_limited"
	case accountUsageVideo:
		return "video_quota_limited"
	case accountUsageMetadata:
		return "metadata_rate_limited"
	case accountUsageUnknown:
		return "legacy_unknown_quota_limited"
	default:
		return "chat_rate_limited"
	}
}

func inferRateLimitUsage(message string) string {
	lower := strings.ToLower(message)
	switch {
	case strings.Contains(lower, "image") || strings.Contains(lower, "image_gen") || strings.Contains(lower, "t2i") || strings.Contains(lower, "picture") || strings.Contains(lower, "photo") || strings.Contains(lower, "图片") || strings.Contains(lower, "图像") || strings.Contains(lower, "cdn.qwenlm.ai"):
		return accountUsageImage
	case strings.Contains(lower, "video") || strings.Contains(lower, "t2v") || strings.Contains(lower, ".mp4") || strings.Contains(lower, "视频"):
		return accountUsageVideo
	case strings.Contains(lower, "chat") || strings.Contains(lower, "t2t") || strings.Contains(lower, "message") || strings.Contains(lower, "completion") || strings.Contains(lower, "对话"):
		return accountUsageChat
	default:
		return accountUsageUnknown
	}
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// ---- migrated from auth_browser.go ----
const (
	mailBaseURL = "https://mail.chatgpt.org.uk"
)

var (
	browserAutomationMu sync.Mutex
	playwrightInstallMu sync.Mutex
	playwrightInstalled bool
	mailLinkKeywords    = []string{"qwen", "verify", "activate", "confirm", "aliyun", "alibaba", "qwenlm"}
)

type MailSession struct {
	http      *http.Client
	token     string
	expiresAt int64
}

func installPlaywrightBrowsers(logger *slog.Logger) error {
	playwrightInstallMu.Lock()
	defer playwrightInstallMu.Unlock()
	if playwrightInstalled {
		return nil
	}
	if logger != nil {
		logger.Info("installing Playwright browser runtime", "browser", "chromium")
	}
	err := pw.Install(&pw.RunOptions{
		Browsers: []string{"chromium"},
		Stdout:   io.Discard,
		Stderr:   io.Discard,
	})
	if err != nil {
		return err
	}
	playwrightInstalled = true
	return nil
}

func NewMailSession() *MailSession {
	return &MailSession{http: &http.Client{Timeout: 20 * time.Second}}
}

func (m *MailSession) init(ctx context.Context) bool {
	status, text, err := m.mailRequest(ctx, http.MethodGet, "/", nil, map[string]string{"referer": mailBaseURL + "/"})
	if err == nil && status == http.StatusOK {
		re := regexp.MustCompile(`window\.__BROWSER_AUTH\s*=\s*(\{[^}]+\})`)
		if match := re.FindStringSubmatch(text); len(match) >= 2 {
			var auth map[string]any
			if json.Unmarshal([]byte(match[1]), &auth) == nil {
				m.setAuth(auth)
				if m.token != "" {
					return true
				}
			}
		}
	}
	status, text, err = m.mailRequest(ctx, http.MethodGet, "/api/auth/token", nil, map[string]string{"referer": mailBaseURL + "/"})
	if err != nil || status != http.StatusOK {
		return false
	}
	var data map[string]any
	if json.Unmarshal([]byte(text), &data) != nil {
		return false
	}
	m.setAuth(data)
	return m.token != ""
}

func (m *MailSession) ensureToken(ctx context.Context) bool {
	if m.token == "" || time.Now().Unix() > m.expiresAt-120 {
		return m.init(ctx)
	}
	return true
}

func (m *MailSession) setAuth(auth map[string]any) {
	if auth == nil {
		return
	}
	if token := strings.TrimSpace(anyString(auth["token"], "")); token != "" {
		m.token = token
	}
	switch v := auth["expires_at"].(type) {
	case float64:
		m.expiresAt = int64(v)
	case json.Number:
		if n, err := v.Int64(); err == nil {
			m.expiresAt = n
		}
	}
}

func (m *MailSession) refreshMailboxToken(ctx context.Context, email string) bool {
	email = strings.TrimSpace(strings.ToLower(email))
	if email == "" {
		return false
	}
	body := map[string]any{"email": email}
	status, text, err := m.mailRequest(ctx, http.MethodPost, "/api/inbox-token", body, map[string]string{
		"content-type": "application/json",
		"referer":      mailBaseURL + "/" + email,
	})
	if err != nil || status != http.StatusOK {
		return false
	}
	var data map[string]any
	if json.Unmarshal([]byte(text), &data) != nil || data["success"] != true {
		return false
	}
	auth, _ := data["auth"].(map[string]any)
	m.setAuth(auth)
	return m.token != ""
}

func (m *MailSession) PollVerifyLink(ctx context.Context, email string, timeout time.Duration) string {
	email = strings.TrimSpace(strings.ToLower(email))
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if ctx.Err() != nil {
			return ""
		}
		if !m.refreshMailboxToken(ctx, email) && !m.ensureToken(ctx) {
			sleepWithContext(ctx, 2*time.Second)
			continue
		}
		link := m.fetchVerifyLink(ctx, email)
		if link != "" {
			return link
		}
		sleepWithContext(ctx, 2*time.Second)
	}
	return ""
}

func (m *MailSession) fetchVerifyLink(ctx context.Context, email string) string {
	path := "/api/emails?email=" + urlQueryEscape(email)
	status, text, err := m.mailRequest(ctx, http.MethodGet, path, nil, map[string]string{
		"accept":        "*/*",
		"referer":       mailBaseURL + "/" + email,
		"x-inbox-token": m.token,
	})
	if err != nil {
		return ""
	}
	var data map[string]any
	if json.Unmarshal([]byte(text), &data) == nil {
		if auth, ok := data["auth"].(map[string]any); ok {
			m.setAuth(auth)
		}
	}
	if status != http.StatusOK {
		return ""
	}
	root, _ := data["data"].(map[string]any)
	for _, raw := range anyList(root["emails"]) {
		msg, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if link := extractVerifyLinkFromEmailRecord(msg); link != "" {
			return link
		}
	}
	return ""
}

func (m *MailSession) mailRequest(ctx context.Context, method, path string, body any, headers map[string]string) (int, string, error) {
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return 0, "", err
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, mailBaseURL+path, reader)
	if err != nil {
		return 0, "", err
	}
	req.Header.Set("user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0")
	req.Header.Set("accept-language", "zh-CN,zh;q=0.9,en;q=0.8")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := m.http.Do(req)
	if err != nil {
		return 0, "", err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(raw), nil
}

func extractVerifyLinkFromEmailRecord(msg map[string]any) string {
	subject := strings.ToLower(anyString(msg["subject"], ""))
	parts := []string{}
	for _, field := range []string{"html_content", "content", "body", "html", "text", "raw"} {
		if value := anyString(msg[field], ""); value != "" {
			parts = append(parts, value)
		}
	}
	for _, field := range []string{"payload", "data", "message"} {
		switch v := msg[field].(type) {
		case map[string]any:
			for _, inner := range v {
				if s := anyString(inner, ""); s != "" {
					parts = append(parts, s)
				}
			}
		case string:
			parts = append(parts, v)
		}
	}
	combined := html.UnescapeString(strings.Join(parts, " "))
	combined = strings.ReplaceAll(combined, `\u003c`, "<")
	combined = strings.ReplaceAll(combined, `\u003e`, ">")
	combined = strings.ReplaceAll(combined, `\u0026`, "&")
	combined = strings.ReplaceAll(combined, `\/`, "/")

	links := []string{}
	for _, re := range []*regexp.Regexp{
		regexp.MustCompile(`(?i)href=["'](https?://[^"']+)["']`),
		regexp.MustCompile(`https?://[^\s"'<>,)]+`),
	} {
		for _, match := range re.FindAllStringSubmatch(combined, -1) {
			link := match[len(match)-1]
			links = append(links, strings.TrimRight(link, ".,;)"))
		}
	}
	for _, link := range links {
		if hasMailKeyword(link) {
			return link
		}
	}
	if hasMailKeyword(subject) {
		for _, link := range links {
			if strings.HasPrefix(link, "http") {
				return link
			}
		}
	}
	return ""
}

func (app *App) activateQwenAccount(ctx context.Context, acc Account) (Account, bool, error) {
	browserAutomationMu.Lock()
	defer browserAutomationMu.Unlock()

	if app.logger != nil {
		app.logger.Info("账号激活流程开始", "account", acc.Email)
	}
	mail := NewMailSession()
	verifyLink := mail.PollVerifyLink(ctx, acc.Email, 30*time.Second)
	if app.logger != nil {
		app.logger.Info("账号激活邮件轮询完成", "account", acc.Email, "link_found", verifyLink != "")
	}
	err := app.withBrowser(ctx, func(page pw.Page) error {
		if verifyLink == "" {
			if app.logger != nil {
				app.logger.Info("账号激活尝试从邮箱页面查找链接", "account", acc.Email)
			}
			verifyLink = findVerifyLinkViaMailPage(ctx, page, acc.Email)
		}
		if verifyLink == "" {
			if app.logger != nil {
				app.logger.Warn("账号激活未找到验证链接", "account", acc.Email)
			}
			return errors.New("activation email not found")
		}
		if app.logger != nil {
			app.logger.Info("账号激活开始访问验证链接", "account", acc.Email)
		}
		if _, err := page.Goto(verifyLink, pw.PageGotoOptions{WaitUntil: pw.WaitUntilStateDomcontentloaded, Timeout: pw.Float(30000)}); err != nil {
			app.logger.Warn("activation link load failed", "error", err)
		}
		sleepWithContext(ctx, 5*time.Second)
		token := localStorageToken(page)
		if token == "" && acc.Password != "" {
			if app.logger != nil {
				app.logger.Info("账号激活验证链接未直接返回 token，尝试网页登录", "account", acc.Email)
			}
			token = loginAndGetToken(ctx, page, acc.Email, acc.Password, 30*time.Second)
		}
		if token != "" {
			acc.Token = token
			cookies := qwenCookieString(page)
			acc.Cookies = cookies
			acc.Valid = true
			acc.ActivationPending = false
			acc.StatusCode = "valid"
			acc.LastError = ""
			if app.logger != nil {
				app.logger.Info("账号激活获取 token 成功", "account", acc.Email, "cookies_found", cookies != "")
			}
			return nil
		}
		if app.client.VerifyToken(ctx, qwenCreds(acc.Token, acc.Cookies)) {
			acc.Valid = true
			acc.ActivationPending = false
			acc.StatusCode = "valid"
			acc.LastError = ""
			if app.logger != nil {
				app.logger.Info("账号激活未获取新 token，但现有 token 验证通过", "account", acc.Email)
			}
			return nil
		}
		if app.logger != nil {
			app.logger.Warn("账号激活访问完成但未获取 token", "account", acc.Email)
		}
		return errors.New("activation link visited but token was not available")
	})
	if err != nil {
		if app.logger != nil {
			app.logger.Warn("账号激活流程失败", "account", acc.Email, "error", err)
		}
		return acc, false, err
	}
	if app.logger != nil {
		app.logger.Info("账号激活流程成功", "account", acc.Email)
	}
	return acc, true, nil
}

func (app *App) withBrowser(ctx context.Context, fn func(page pw.Page) error) error {
	if err := installPlaywrightBrowsers(app.logger); err != nil {
		return fmt.Errorf("playwright install failed: %w", err)
	}
	runner, err := pw.Run(&pw.RunOptions{SkipInstallBrowsers: true})
	if err != nil {
		return fmt.Errorf("playwright run failed: %w", err)
	}
	defer runner.Stop()

	browser, err := runner.Chromium.Launch(pw.BrowserTypeLaunchOptions{
		Headless: pw.Bool(true),
		Timeout:  pw.Float(60000),
		Args: []string{
			"--disable-dev-shm-usage",
			"--disable-blink-features=AutomationControlled",
			"--no-sandbox",
		},
	})
	if err != nil {
		return fmt.Errorf("browser launch failed: %w", err)
	}
	defer browser.Close()

	context, err := browser.NewContext(pw.BrowserNewContextOptions{
		UserAgent: pw.String("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0"),
		Locale:    pw.String("zh-CN"),
		Viewport:  &pw.Size{Width: 1365, Height: 768},
		ExtraHttpHeaders: map[string]string{
			"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
		},
	})
	if err != nil {
		return fmt.Errorf("browser context failed: %w", err)
	}
	defer context.Close()

	page, err := context.NewPage()
	if err != nil {
		return fmt.Errorf("browser page failed: %w", err)
	}
	page.SetDefaultTimeout(30000)
	page.SetDefaultNavigationTimeout(60000)
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return fn(page)
}

func (app *App) refreshAccountSession(ctx context.Context, acc Account) (Account, error) {
	if strings.TrimSpace(acc.Password) == "" {
		return acc, errors.New("password required to refresh cookies")
	}
	browserAutomationMu.Lock()
	defer browserAutomationMu.Unlock()
	err := app.withBrowser(ctx, func(page pw.Page) error {
		token := loginAndGetToken(ctx, page, acc.Email, acc.Password, 30*time.Second)
		if token == "" {
			return errors.New("browser login failed: token not found")
		}
		acc.Token = token
		acc.Cookies = qwenCookieString(page)
		if acc.Cookies == "" {
			return errors.New("browser login succeeded but cookies were empty")
		}
		acc.Valid = true
		acc.ActivationPending = false
		acc.StatusCode = "valid"
		acc.LastError = ""
		return nil
	})
	if err != nil {
		return acc, err
	}
	if app.logger != nil {
		app.logger.Info("账号会话刷新成功", "account", acc.Email, "cookies_found", acc.Cookies != "")
	}
	return acc, nil
}

func loginAndGetToken(ctx context.Context, page pw.Page, email, password string, timeout time.Duration) string {
	_, _ = page.Goto(qwenBaseURL+"/auth", pw.PageGotoOptions{WaitUntil: pw.WaitUntilStateDomcontentloaded, Timeout: pw.Float(30000)})
	sleepWithContext(ctx, 2*time.Second)
	if !fillFirst(page, []string{`input[placeholder*="Email"]`, `input[type="email"]`}, email, 10000) {
		_ = fillNthInput(page, 0, email)
	}
	if !fillFirst(page, []string{`input[type="password"]`, `input[placeholder*="Password"]`}, password, 10000) {
		_ = fillPasswordInput(page, 0, password)
	}
	if !clickFirst(page, []string{`button:has-text("Log in")`, `button[type="submit"]:not([disabled])`, `button[type="submit"]`, `button:has-text("Continue")`}, 5000) {
		_ = page.Press(`input[type="password"]`, "Enter")
	}
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if ctx.Err() != nil {
			return ""
		}
		if token := localStorageToken(page); token != "" {
			return token
		}
		sleepWithContext(ctx, time.Second)
	}
	return ""
}

func fillFirst(page pw.Page, selectors []string, value string, timeoutMS float64) bool {
	for _, selector := range selectors {
		if _, err := page.WaitForSelector(selector, pw.PageWaitForSelectorOptions{Timeout: pw.Float(timeoutMS)}); err != nil {
			continue
		}
		if err := page.Fill(selector, value, pw.PageFillOptions{Timeout: pw.Float(5000)}); err == nil {
			return true
		}
	}
	return false
}

func clickFirst(page pw.Page, selectors []string, timeoutMS float64) bool {
	for _, selector := range selectors {
		if err := page.Click(selector, pw.PageClickOptions{Timeout: pw.Float(timeoutMS), Force: pw.Bool(true)}); err == nil {
			return true
		}
	}
	return false
}

func fillNthInput(page pw.Page, index int, value string) error {
	_, err := page.Evaluate(`([index, value]) => {
		const inputs = Array.from(document.querySelectorAll('input'));
		const el = inputs[index];
		if (!el) return false;
		el.focus();
		el.value = value;
		el.dispatchEvent(new Event('input', { bubbles: true }));
		el.dispatchEvent(new Event('change', { bubbles: true }));
		return true;
	}`, []any{index, value})
	return err
}

func fillPasswordInput(page pw.Page, index int, value string) error {
	_, err := page.Evaluate(`([index, value]) => {
		const inputs = Array.from(document.querySelectorAll('input[type="password"]'));
		const el = inputs[index];
		if (!el) return false;
		el.focus();
		el.value = value;
		el.dispatchEvent(new Event('input', { bubbles: true }));
		el.dispatchEvent(new Event('change', { bubbles: true }));
		return true;
	}`, []any{index, value})
	return err
}

func localStorageToken(page pw.Page) string {
	raw, err := page.Evaluate(`() => localStorage.getItem('token')`)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(anyString(raw, ""))
}

func qwenCookieString(page pw.Page) string {
	cookies, err := page.Context().Cookies(qwenBaseURL)
	if err != nil {
		return ""
	}
	parts := []string{}
	for _, cookie := range cookies {
		if strings.Contains(cookie.Domain, "qwen") && cookie.Name != "" {
			parts = append(parts, cookie.Name+"="+cookie.Value)
		}
	}
	return strings.Join(parts, "; ")
}

func findVerifyLinkViaMailPage(ctx context.Context, page pw.Page, email string) string {
	_, _ = page.Goto(mailBaseURL+"/"+strings.TrimSpace(email), pw.PageGotoOptions{WaitUntil: pw.WaitUntilStateDomcontentloaded, Timeout: pw.Float(30000)})
	sleepWithContext(ctx, 6*time.Second)
	_, _ = page.Evaluate(`() => {
		const modal = document.querySelector('#siteAnnouncementModal');
		if (modal) {
			modal.classList.remove('active');
			modal.setAttribute('aria-hidden', 'true');
			modal.style.display = 'none';
			modal.style.pointerEvents = 'none';
		}
		document.querySelectorAll('.modal-overlay, .announcement-modal-overlay').forEach((el) => {
			el.classList.remove('active');
			el.setAttribute('aria-hidden', 'true');
			el.style.display = 'none';
			el.style.pointerEvents = 'none';
		});
	}`)
	for _, selector := range []string{"#emailList li:first-child", "#emailList li", `[class*="EmailItem"]`, `[class*="email-item"]`, `[class*="MailItem"]`, `[class*="mail-item"]`, "table tbody tr:first-child", `[role="row"]:first-child`} {
		if clickFirst(page, []string{selector}, 4000) {
			sleepWithContext(ctx, 4*time.Second)
			if link := extractVerifyLinkFromPage(page); link != "" {
				return link
			}
		}
	}
	return extractVerifyLinkFromPage(page)
}

func extractVerifyLinkFromPage(page pw.Page) string {
	js := `() => {
		const keywords = ['qwen', 'verify', 'activate', 'confirm', 'aliyun', 'alibaba', 'qwenlm'];
		const links = Array.from(document.querySelectorAll('a[href]'));
		for (const link of links) {
			const href = link.href || '';
			const text = (link.textContent || '').toLowerCase();
			if (keywords.some((keyword) => href.toLowerCase().includes(keyword))) return href;
			if (keywords.some((keyword) => text.includes(keyword)) && href.startsWith('http')) return href;
		}
		const html = document.body ? document.body.innerHTML : '';
		const matches = html.match(/https?:\/\/[^"'\s<>\\]+/g) || [];
		for (const match of matches) {
			if (keywords.some((keyword) => match.toLowerCase().includes(keyword))) return match;
		}
		return '';
	}`
	if iframe, err := page.QuerySelector("#emailFrame"); err == nil && iframe != nil {
		if frame, err := iframe.ContentFrame(); err == nil && frame != nil {
			if raw, err := frame.Evaluate(js); err == nil {
				if link := strings.TrimSpace(anyString(raw, "")); link != "" {
					return link
				}
			}
		}
	}
	raw, err := page.Evaluate(js)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(anyString(raw, ""))
}

func sleepWithContext(ctx context.Context, delay time.Duration) {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-timer.C:
	}
}

func randomBytes(buf []byte) {
	if _, err := cryptorand.Read(buf); err != nil {
		now := time.Now().UnixNano()
		for i := range buf {
			buf[i] = byte(now >> (i % 8))
		}
	}
}

func hasMailKeyword(value string) bool {
	lower := strings.ToLower(value)
	for _, keyword := range mailLinkKeywords {
		if strings.Contains(lower, keyword) {
			return true
		}
	}
	return false
}

func urlQueryEscape(value string) string {
	replacer := strings.NewReplacer(" ", "%20", "@", "%40", "+", "%2B", "&", "%26", "?", "%3F", "#", "%23")
	return replacer.Replace(value)
}

// ---- migrated from chat_id_pool.go ----
type WarmChat struct {
	Email     string
	Token     string
	Cookies   string
	Model     string
	ChatType  string
	ChatID    string
	CreatedAt time.Time
}

type ChatIDPool struct {
	client   *QwenClient
	accounts *AccountPool
	settings Settings
	logger   *slog.Logger

	mu      sync.Mutex
	items   map[string][]WarmChat
	desired map[string]ModelWarmKey
	filling bool
}

type ModelWarmKey struct {
	Model    string
	ChatType string
}

func NewChatIDPool(client *QwenClient, accounts *AccountPool, settings Settings, logger *slog.Logger) *ChatIDPool {
	pool := &ChatIDPool{
		client:   client,
		accounts: accounts,
		settings: settings,
		logger:   logger,
		items:    map[string][]WarmChat{},
		desired:  map[string]ModelWarmKey{},
	}
	pool.RememberModel("qwen3.6-plus", "t2t")
	return pool
}

func (p *ChatIDPool) Start(ctx context.Context) {
	if p == nil {
		return
	}
	if p.prewarmEnabled() {
		settings := p.settingsSnapshot()
		logInfo(p.logger, ctx, "启动 Chat_ID 预热池",
			"target_per_account", settings.ChatIDPrewarmTargetPerAccount,
			"ttl_seconds", settings.ChatIDPrewarmTTLSeconds,
			"max_concurrency", settings.ChatIDPrewarmMaxConcurrency,
		)
	}
	go p.loop(ctx)
}

func (p *ChatIDPool) loop(ctx context.Context) {
	p.Fill(ctx)
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			if p.prewarmEnabled() {
				p.cleanup(context.Background(), true)
				logInfo(p.logger, ctx, "停止 Chat_ID 预热池")
			}
			return
		case <-ticker.C:
			p.Fill(ctx)
		}
	}
}

func (p *ChatIDPool) settingsSnapshot() Settings {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.settings
}

func (p *ChatIDPool) prewarmEnabled() bool {
	if p == nil {
		return false
	}
	return p.settingsSnapshot().ChatIDPrewarmTargetPerAccount > 0
}

func (p *ChatIDPool) UpdateSettings(settings Settings) {
	if p == nil {
		return
	}
	p.mu.Lock()
	wasEnabled := p.settings.ChatIDPrewarmTargetPerAccount > 0
	p.settings = settings
	isEnabled := p.settings.ChatIDPrewarmTargetPerAccount > 0
	p.mu.Unlock()
	if !wasEnabled && isEnabled {
		logInfo(p.logger, context.Background(), "启用 Chat_ID 预热池",
			"target_per_account", settings.ChatIDPrewarmTargetPerAccount,
			"ttl_seconds", settings.ChatIDPrewarmTTLSeconds,
			"max_concurrency", settings.ChatIDPrewarmMaxConcurrency,
		)
	}
}

func (p *ChatIDPool) RememberModel(model, chatType string) {
	if p == nil {
		return
	}
	model = strings.TrimSpace(model)
	if model == "" {
		model = "qwen3.6-plus"
	}
	chatType = normalizeUpstreamChatType(chatType)
	key := model + "|" + chatType
	added := false
	p.mu.Lock()
	if _, ok := p.desired[key]; !ok {
		added = true
	}
	p.desired[key] = ModelWarmKey{Model: model, ChatType: chatType}
	p.mu.Unlock()
	if added {
		p.triggerAsyncFill()
	}
}

func (p *ChatIDPool) Take(ctx context.Context, email, model, chatType string) (string, bool) {
	if p == nil || email == "" {
		return "", false
	}
	p.RememberModel(model, chatType)
	p.cleanup(ctx, false)
	key := warmChatKey(email, model, chatType)
	p.mu.Lock()
	items := p.items[key]
	if len(items) == 0 {
		p.mu.Unlock()
		p.triggerAsyncFill()
		return "", false
	}
	item := items[0]
	p.items[key] = items[1:]
	remaining := len(p.items[key])
	p.mu.Unlock()
	setRequestLogFields(ctx, "chat_id", item.ChatID)
	logInfo(p.logger, ctx, "复用预热会话", "warm_key", key, "cached_remaining", remaining)
	return item.ChatID, true
}

func (p *ChatIDPool) triggerAsyncFill() {
	if p == nil || !p.prewarmEnabled() {
		return
	}
	go p.Fill(context.Background())
}

func (p *ChatIDPool) Fill(ctx context.Context) {
	if p == nil || p.client == nil || p.accounts == nil {
		return
	}
	p.mu.Lock()
	settings := p.settings
	if settings.ChatIDPrewarmTargetPerAccount <= 0 {
		p.mu.Unlock()
		return
	}
	if p.filling {
		p.mu.Unlock()
		return
	}
	p.filling = true
	desired := make([]ModelWarmKey, 0, len(p.desired))
	for _, item := range p.desired {
		desired = append(desired, item)
	}
	p.mu.Unlock()
	defer func() {
		p.mu.Lock()
		p.filling = false
		p.mu.Unlock()
	}()

	p.cleanup(ctx, false)
	accounts := p.accounts.Snapshot()
	target := maxInt(settings.ChatIDPrewarmTargetPerAccount, 0)
	sem := make(chan struct{}, maxInt(settings.ChatIDPrewarmMaxConcurrency, 1))
	var wg sync.WaitGroup
	for _, acc := range accounts {
		if !acc.available(settings) {
			continue
		}
		for _, desiredKey := range desired {
			missing := target - p.count(acc.Email, desiredKey.Model, desiredKey.ChatType)
			for i := 0; i < missing; i++ {
				select {
				case <-ctx.Done():
					wg.Wait()
					return
				case sem <- struct{}{}:
				}
				wg.Add(1)
				go func(account Account, warmKey ModelWarmKey) {
					defer func() {
						<-sem
						wg.Done()
					}()
					p.createWarmChat(ctx, account, warmKey)
				}(acc, desiredKey)
			}
		}
	}
	wg.Wait()
}

func (p *ChatIDPool) createWarmChat(ctx context.Context, acc Account, warmKey ModelWarmKey) {
	fillCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	chatID, err := p.client.CreateChat(fillCtx, qwenCreds(acc.Token, acc.Cookies), warmKey.Model, warmKey.ChatType)
	if err != nil {
		logWarn(p.logger, ctx, "预热会话创建失败", "account", acc.Email, "model", warmKey.Model, "chat_type", warmKey.ChatType, "error", err)
		return
	}
	item := WarmChat{
		Email: acc.Email, Token: acc.Token, Cookies: acc.Cookies, Model: warmKey.Model, ChatType: normalizeUpstreamChatType(warmKey.ChatType),
		ChatID: chatID, CreatedAt: time.Now(),
	}
	key := warmChatKey(item.Email, item.Model, item.ChatType)
	p.mu.Lock()
	p.items[key] = append(p.items[key], item)
	cached := len(p.items[key])
	p.mu.Unlock()
	logInfo(p.logger, ctx, "预热会话创建成功", "account", acc.Email, "chat_id", chatID, "model", warmKey.Model, "chat_type", warmKey.ChatType, "cached", cached)
}

func (p *ChatIDPool) count(email, model, chatType string) int {
	key := warmChatKey(email, model, chatType)
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.items[key])
}

func (p *ChatIDPool) cleanup(ctx context.Context, deleteAll bool) {
	if p == nil {
		return
	}
	ttl := time.Duration(maxInt(p.settings.ChatIDPrewarmTTLSeconds, 1)) * time.Second
	now := time.Now()
	expired := []WarmChat{}
	p.mu.Lock()
	for key, items := range p.items {
		next := items[:0]
		for _, item := range items {
			if deleteAll || now.Sub(item.CreatedAt) >= ttl {
				expired = append(expired, item)
				continue
			}
			next = append(next, item)
		}
		if len(next) == 0 {
			delete(p.items, key)
		} else {
			p.items[key] = next
		}
	}
	p.mu.Unlock()
	for _, item := range expired {
		p.client.DeleteChat(context.Background(), qwenCreds(item.Token, item.Cookies), item.ChatID)
	}
	if len(expired) > 0 {
		logInfo(p.logger, ctx, "清理预热会话", "count", len(expired), "delete_all", deleteAll)
	}
}

func (p *ChatIDPool) Status() map[string]any {
	perAccount := map[string]int{}
	total := 0
	settings := Settings{}
	if p != nil {
		settings = p.settings
		p.cleanup(context.Background(), false)
		p.mu.Lock()
		for _, items := range p.items {
			total += len(items)
			for _, item := range items {
				perAccount[item.Email]++
			}
		}
		p.mu.Unlock()
	}
	return map[string]any{
		"total_cached":       total,
		"target_per_account": settings.ChatIDPrewarmTargetPerAccount,
		"ttl_seconds":        settings.ChatIDPrewarmTTLSeconds,
		"max_concurrency":    settings.ChatIDPrewarmMaxConcurrency,
		"per_account":        perAccount,
	}
}

func warmChatKey(email, model, chatType string) string {
	return fmt.Sprintf("%s|%s|%s", email, strings.TrimSpace(model), normalizeUpstreamChatType(chatType))
}

// ---- migrated from config.go ----
const (
	Version                  = "2.0.0-go"
	openAIModelCreatedEpoch  = int64(1700000000)
	keepAliveMinInterval     = 5
	keepAliveMaxInterval     = 86400
	keepAliveDefaultInterval = 60
)

var (
	numberedAPIKeyEnvRe  = regexp.MustCompile(`^QWEN_API_KEY_(\d+)$`)
	numberedAccountEnvRe = regexp.MustCompile(`^QWEN_ACCOUNT_(\d+)$`)
)

type Settings struct {
	Port                                   int
	Workers                                int
	AdminKey                               string
	BrowserPoolSize                        int
	MaxInflightPerAccount                  int
	BrowserStreamTimeoutSeconds            int
	UpstreamStreamHeaderTimeoutSeconds     int
	UpstreamStreamFirstEventTimeoutSeconds int
	UpstreamStreamIdleTimeoutSeconds       int
	MaxRetries                             int
	RateLimitCooldown                      int
	AccountMinIntervalMS                   int
	RequestJitterMinMS                     int
	RequestJitterMaxMS                     int
	RateLimitBaseCooldown                  int
	RateLimitMaxCooldown                   int
	AccountReadySetThreshold               int
	ChatDeleteRetryAttempts                int
	ChatDeleteRetryDelaySeconds            float64
	ChatIDPrewarmTargetPerAccount          int
	ChatIDPrewarmTTLSeconds                int
	ChatIDPrewarmMaxConcurrency            int
	TraceResponseFingerprints              bool
	TraceResponseTailChars                 int
	LogLevel                               string
	KeepAliveURL                           string
	KeepAliveInterval                      int

	BaseDir                          string
	DataDir                          string
	LogsDir                          string
	AccountsFile                     string
	UsersFile                        string
	CapturesFile                     string
	ConfigFile                       string
	APIKeysFile                      string
	ContextInlineMaxChars            int
	ContextForceFileMaxChars         int
	ContextAttachmentTTLSeconds      int
	ContextUploadParseTimeoutSeconds int
	ContextGeneratedDir              string
	ContextCacheFile                 string
	UploadedFilesFile                string
	ContextAffinityFile              string
	ContextAllowedGeneratedExts      string
	ContextAllowedUserExts           string
	FrontendDist                     string
}

func LoadSettings() Settings {
	base := repoRootFromCwd()
	if v := strings.TrimSpace(os.Getenv("BASE_DIR")); v != "" {
		base = filepath.Clean(v)
		if abs, err := filepath.Abs(base); err == nil {
			base = abs
		}
	}
	data := envString("DATA_DIR", filepath.Join(base, "data"))
	logs := envString("LOGS_DIR", filepath.Join(base, "logs"))
	settings := Settings{
		Port:                                   envInt("PORT", 7860),
		Workers:                                envInt("WORKERS", 1),
		AdminKey:                               envString("ADMIN_KEY", ""),
		BrowserPoolSize:                        envInt("BROWSER_POOL_SIZE", 1),
		MaxInflightPerAccount:                  envIntAlias("MAX_INFLIGHT_PER_ACCOUNT", "MAX_INFLIGHT", 2),
		BrowserStreamTimeoutSeconds:            envInt("BROWSER_STREAM_TIMEOUT_SECONDS", 1800),
		UpstreamStreamHeaderTimeoutSeconds:     envInt("UPSTREAM_STREAM_HEADER_TIMEOUT_SECONDS", 120),
		UpstreamStreamFirstEventTimeoutSeconds: envInt("UPSTREAM_STREAM_FIRST_EVENT_TIMEOUT_SECONDS", 180),
		UpstreamStreamIdleTimeoutSeconds:       envInt("UPSTREAM_STREAM_IDLE_TIMEOUT_SECONDS", 180),
		MaxRetries:                             envInt("MAX_RETRIES", 3),
		RateLimitCooldown:                      600,
		AccountMinIntervalMS:                   envInt("ACCOUNT_MIN_INTERVAL_MS", 0),
		RequestJitterMinMS:                     envInt("REQUEST_JITTER_MIN_MS", 0),
		RequestJitterMaxMS:                     envInt("REQUEST_JITTER_MAX_MS", 0),
		RateLimitBaseCooldown:                  envInt("RATE_LIMIT_BASE_COOLDOWN", 600),
		RateLimitMaxCooldown:                   envInt("RATE_LIMIT_MAX_COOLDOWN", 3600),
		AccountReadySetThreshold:               envInt("ACCOUNT_READY_SET_THRESHOLD", 128),
		ChatDeleteRetryAttempts:                envInt("CHAT_DELETE_RETRY_ATTEMPTS", 3),
		ChatDeleteRetryDelaySeconds:            envFloat("CHAT_DELETE_RETRY_DELAY_SECONDS", 0.5),
		ChatIDPrewarmTargetPerAccount:          envInt("CHAT_ID_PREWARM_TARGET_PER_ACCOUNT", 5),
		ChatIDPrewarmTTLSeconds:                envInt("CHAT_ID_PREWARM_TTL_SECONDS", 120),
		ChatIDPrewarmMaxConcurrency:            envInt("CHAT_ID_PREWARM_MAX_CONCURRENCY", 16),
		TraceResponseFingerprints:              envBool("TRACE_RESPONSE_FINGERPRINTS", false),
		TraceResponseTailChars:                 envInt("TRACE_RESPONSE_TAIL_CHARS", 160),
		LogLevel:                               envString("LOG_LEVEL", "INFO"),
		KeepAliveURL:                           envString("KEEPALIVE_URL", ""),
		KeepAliveInterval:                      clampInt(envInt("KEEPALIVE_INTERVAL", keepAliveDefaultInterval), keepAliveMinInterval, keepAliveMaxInterval),
		BaseDir:                                base,
		DataDir:                                data,
		LogsDir:                                logs,
		AccountsFile:                           envString("ACCOUNTS_FILE", filepath.Join(data, "accounts.json")),
		UsersFile:                              envString("USERS_FILE", filepath.Join(data, "users.json")),
		CapturesFile:                           envString("CAPTURES_FILE", filepath.Join(data, "captures.json")),
		ConfigFile:                             envString("CONFIG_FILE", filepath.Join(data, "config.json")),
		APIKeysFile:                            filepath.Join(data, "api_keys.json"),
		ContextInlineMaxChars:                  envInt("CONTEXT_INLINE_MAX_CHARS", 4000),
		ContextForceFileMaxChars:               envInt("CONTEXT_FORCE_FILE_MAX_CHARS", 10000),
		ContextAttachmentTTLSeconds:            envInt("CONTEXT_ATTACHMENT_TTL_SECONDS", 1800),
		ContextUploadParseTimeoutSeconds:       envInt("CONTEXT_UPLOAD_PARSE_TIMEOUT_SECONDS", 60),
		ContextGeneratedDir:                    envString("CONTEXT_GENERATED_DIR", filepath.Join(data, "context_files")),
		ContextCacheFile:                       envString("CONTEXT_CACHE_FILE", filepath.Join(data, "context_cache.json")),
		UploadedFilesFile:                      envString("UPLOADED_FILES_FILE", filepath.Join(data, "uploaded_files.json")),
		ContextAffinityFile:                    envString("CONTEXT_AFFINITY_FILE", filepath.Join(data, "session_affinity.json")),
		ContextAllowedGeneratedExts:            envString("CONTEXT_ALLOWED_GENERATED_EXTS", "txt,md,json,log"),
		ContextAllowedUserExts:                 envString("CONTEXT_ALLOWED_USER_EXTS", "txt,md,json,log,xml,yaml,yml,csv,html,css,py,js,ts,java,c,cpp,cs,php,go,rb,sh,zsh,ps1,bat,cmd,pdf,doc,docx,ppt,pptx,xls,xlsx,png,jpg,jpeg,webp,gif,tiff,bmp,svg"),
		FrontendDist:                           filepath.Join(base, "frontend", "dist"),
	}
	if v := strings.TrimSpace(os.Getenv("API_KEYS_FILE")); v != "" {
		settings.APIKeysFile = v
	}
	return settings
}

var modelMap = map[string]string{
	"gpt-4o": "qwen3.6-plus", "gpt-4o-mini": "qwen3.5-flash", "gpt-4-turbo": "qwen3.6-plus",
	"gpt-4": "qwen3.6-plus", "gpt-4.1": "qwen3.6-plus", "gpt-4.1-mini": "qwen3.5-flash",
	"gpt-3.5-turbo": "qwen3.5-flash", "gpt-5": "qwen3.6-plus", "o1": "qwen3.6-plus",
	"o1-mini": "qwen3.5-flash", "o3": "qwen3.6-plus", "o3-mini": "qwen3.5-flash",
	"claude-opus-4-6": "qwen3.6-plus", "claude-sonnet-4-5": "qwen3.6-plus",
	"claude-3-opus": "qwen3.6-plus", "claude-3.5-sonnet": "qwen3.6-plus",
	"claude-3-sonnet": "qwen3.6-plus", "claude-3-haiku": "qwen3.5-flash",
	"gemini-2.5-pro": "qwen3.6-plus", "gemini-2.5-flash": "qwen3.5-flash",
	"qwen": "qwen3.6-plus", "qwen-max": "qwen3.6-plus", "qwen-plus": "qwen3.6-plus",
	"qwen-turbo": "qwen3.5-flash", "deepseek-chat": "qwen3.6-plus", "deepseek-reasoner": "qwen3.6-plus",
}

func resolveModel(name string) string {
	trimmed := strings.TrimSpace(name)
	if v, ok := modelMap[trimmed]; ok {
		return v
	}
	if v, ok := modelMap[strings.ToLower(trimmed)]; ok {
		return v
	}
	for _, suffix := range modelModeSuffixes() {
		lowered := strings.ToLower(trimmed)
		if strings.HasSuffix(lowered, suffix) && len(trimmed) > len(suffix) {
			base := strings.TrimSpace(trimmed[:len(trimmed)-len(suffix)])
			if mapped := resolveModel(base); mapped != base && mapped != "" {
				return mapped + trimmed[len(trimmed)-len(suffix):]
			}
		}
	}
	return trimmed
}

type ModelMode struct {
	RequestedModel string
	BaseModel      string
	ChatType       string
	ForceThinking  bool
	Mode           string
}

func parseModelMode(modelID, defaultModel string) ModelMode {
	requested := strings.TrimSpace(modelID)
	if requested == "" {
		requested = defaultModel
	}
	lowered := strings.ToLower(requested)
	suffixes := []struct {
		suffix        string
		chatType      string
		forceThinking bool
		mode          string
	}{
		{"-deep-research", "deep_research", false, "deep_research"},
		{"-deep_research", "deep_research", false, "deep_research"},
		{"-web-dev", "web_dev", false, "webdev"},
		{"-thinking", "t2t", true, "thinking"},
		{"-search", "t2t", false, "search"},
		{"-webdev", "web_dev", false, "webdev"},
		{"-image", "t2i", false, "image"},
		{"-video", "t2v", false, "video"},
		{"-slides", "slides", false, "slides"},
		{"-t2i", "t2i", false, "image"},
		{"-t2v", "t2v", false, "video"},
	}
	for _, s := range suffixes {
		if strings.HasSuffix(lowered, s.suffix) {
			return ModelMode{
				RequestedModel: requested,
				BaseModel:      strings.TrimSpace(requested[:len(requested)-len(s.suffix)]),
				ChatType:       s.chatType,
				ForceThinking:  s.forceThinking,
				Mode:           s.mode,
			}
		}
	}
	return ModelMode{RequestedModel: requested, BaseModel: requested, ChatType: "t2t", Mode: "chat"}
}

func modelModeSuffixes() []string {
	return []string{"-deep-research", "-deep_research", "-web-dev", "-thinking", "-search", "-webdev", "-image", "-video", "-slides", "-t2i", "-t2v"}
}

func loadAPIKeys(path string, logger *slog.Logger) (map[string]bool, map[string]bool, map[string]bool) {
	managed := loadManagedAPIKeys(path, logger)
	envKeys := loadEnvAPIKeys()
	all := map[string]bool{}
	for key := range managed {
		all[key] = true
	}
	for key := range envKeys {
		all[key] = true
	}
	return all, managed, envKeys
}

func loadManagedAPIKeys(path string, logger *slog.Logger) map[string]bool {
	keys := map[string]bool{}
	raw, err := os.ReadFile(path)
	if err != nil {
		return keys
	}
	var payload struct {
		Keys any `json:"keys"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		logger.Warn("failed to parse api_keys.json", "error", err)
		return keys
	}
	switch v := payload.Keys.(type) {
	case []any:
		for _, item := range v {
			if key := strings.TrimSpace(fmt.Sprint(item)); key != "" {
				keys[key] = true
			}
		}
	case string:
		for _, key := range splitEnvList(v) {
			keys[key] = true
		}
	}
	return keys
}

func loadEnvAPIKeys() map[string]bool {
	keys := map[string]bool{}
	for _, name := range []string{"QWEN_API_KEY", "QWEN_API_KEYS", "API_KEYS"} {
		for _, key := range splitEnvList(os.Getenv(name)) {
			keys[key] = true
		}
	}
	for _, item := range numberedEnvValues(numberedAPIKeyEnvRe) {
		for _, key := range splitEnvList(item.value) {
			keys[key] = true
		}
	}
	return keys
}

func saveAPIKeys(path string, keys map[string]bool) error {
	list := make([]string, 0, len(keys))
	for k := range keys {
		list = append(list, k)
	}
	sort.Strings(list)
	return writeJSONFile(path, map[string]any{"keys": list})
}

type numberedEnvValue struct {
	index int
	name  string
	value string
}

func numberedEnvValues(pattern *regexp.Regexp) []numberedEnvValue {
	items := []numberedEnvValue{}
	for _, env := range os.Environ() {
		name, value, ok := strings.Cut(env, "=")
		if !ok {
			continue
		}
		match := pattern.FindStringSubmatch(name)
		if len(match) != 2 {
			continue
		}
		index, _ := strconv.Atoi(match[1])
		items = append(items, numberedEnvValue{index: index, name: name, value: value})
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].index != items[j].index {
			return items[i].index < items[j].index
		}
		return items[i].name < items[j].name
	})
	return items
}

func splitEnvList(value string) []string {
	parts := regexp.MustCompile(`[,\s;]+`).Split(value, -1)
	out := []string{}
	seen := map[string]bool{}
	for _, part := range parts {
		item := strings.TrimSpace(part)
		if item == "" || seen[item] {
			continue
		}
		seen[item] = true
		out = append(out, item)
	}
	return out
}

func loadEnvAccounts() []Account {
	accounts := []Account{}
	for _, item := range numberedEnvValues(numberedAccountEnvRe) {
		parts := strings.SplitN(item.value, ";", 3)
		token := strings.TrimSpace(parts[0])
		if token == "" {
			continue
		}
		email := fmt.Sprintf("env_%d@qwen", item.index)
		if len(parts) >= 2 && strings.TrimSpace(parts[1]) != "" {
			email = strings.TrimSpace(parts[1])
		}
		password := ""
		if len(parts) >= 3 {
			password = strings.TrimSpace(parts[2])
		}
		accounts = append(accounts, Account{
			Email:      email,
			Password:   password,
			Token:      token,
			Source:     "env",
			EnvName:    item.name,
			StatusCode: "valid",
		})
	}
	return accounts
}

func clampInt(value, minValue, maxValue int) int {
	if value < minValue {
		return minValue
	}
	if value > maxValue {
		return maxValue
	}
	return value
}

func envString(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return fallback
}

func envIntAlias(key, alias string, fallback int) int {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return envInt(alias, fallback)
}

func envFloat(key string, fallback float64) float64 {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return fallback
}

func envBool(key string, fallback bool) bool {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		switch normalizeLower(v) {
		case "1", "true", "yes", "on":
			return true
		case "0", "false", "no", "off":
			return false
		}
	}
	return fallback
}

type KeepAliveConfig struct {
	URL       string
	Interval  int
	EnvLocked []string
}

type KeepAliveService struct {
	logger *slog.Logger

	mu             sync.Mutex
	parent         context.Context
	cancel         context.CancelFunc
	running        bool
	url            string
	interval       int
	lastStatusCode int
	lastError      string
	lastChecked    float64
}

func NewKeepAliveService(logger *slog.Logger) *KeepAliveService {
	return &KeepAliveService{logger: logger}
}

func (s *KeepAliveService) Start(parent context.Context, cfg KeepAliveConfig) {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.parent = parent
	s.mu.Unlock()
	s.Apply(cfg)
}

func (s *KeepAliveService) Apply(cfg KeepAliveConfig) {
	if s == nil {
		return
	}
	s.mu.Lock()
	if s.cancel != nil {
		s.cancel()
		s.cancel = nil
	}
	parent := s.parent
	if parent == nil {
		parent = context.Background()
	}
	cfg.URL = strings.TrimSpace(cfg.URL)
	cfg.Interval = clampInt(cfg.Interval, keepAliveMinInterval, keepAliveMaxInterval)
	s.url = cfg.URL
	s.interval = cfg.Interval
	s.lastError = ""
	s.lastStatusCode = 0
	s.lastChecked = 0
	if cfg.URL == "" {
		s.running = false
		s.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(parent)
	s.cancel = cancel
	s.running = true
	url := cfg.URL
	interval := cfg.Interval
	s.mu.Unlock()

	go s.run(ctx, url, interval)
}

func (s *KeepAliveService) Stop() {
	if s == nil {
		return
	}
	s.mu.Lock()
	if s.cancel != nil {
		s.cancel()
		s.cancel = nil
	}
	s.running = false
	s.mu.Unlock()
}

func (s *KeepAliveService) run(ctx context.Context, target string, interval int) {
	client := &http.Client{Timeout: 30 * time.Second}
	ticker := time.NewTicker(time.Duration(interval) * time.Second)
	defer ticker.Stop()
	for {
		s.ping(ctx, client, target)
		select {
		case <-ctx.Done():
			s.mu.Lock()
			if s.url == target {
				s.running = false
			}
			s.mu.Unlock()
			return
		case <-ticker.C:
		}
	}
}

func (s *KeepAliveService) ping(ctx context.Context, client *http.Client, target string) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		s.record(0, err)
		return
	}
	resp, err := client.Do(req)
	if err != nil {
		if !errors.Is(ctx.Err(), context.Canceled) {
			s.record(0, err)
		}
		return
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	_ = resp.Body.Close()
	s.record(resp.StatusCode, nil)
}

func (s *KeepAliveService) record(status int, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lastStatusCode = status
	s.lastChecked = float64(time.Now().UnixNano()) / 1e9
	if err != nil {
		s.lastError = err.Error()
		if s.logger != nil {
			s.logger.Warn("keepalive request failed", "error", err)
		}
		return
	}
	s.lastError = ""
	if s.logger != nil {
		s.logger.Debug("keepalive request completed", "status", status)
	}
}

func (s *KeepAliveService) Status() map[string]any {
	if s == nil {
		return map[string]any{"running": false}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return map[string]any{
		"running":          s.running,
		"url":              s.url,
		"interval":         s.interval,
		"last_status_code": s.lastStatusCode,
		"last_error":       s.lastError,
		"last_checked":     s.lastChecked,
	}
}

func (app *App) keepaliveConfig() KeepAliveConfig {
	cfg := KeepAliveConfig{URL: app.settings.KeepAliveURL, Interval: app.settings.KeepAliveInterval}
	if cfg.Interval <= 0 {
		cfg.Interval = keepAliveDefaultInterval
	}
	var data map[string]any
	if app.configStore != nil {
		_ = app.configStore.LoadInto(&data)
	}
	if cfg.URL == "" && data != nil {
		cfg.URL = stringValue(data, "keepalive_url", "")
	}
	if data != nil {
		cfg.Interval = clampInt(intValue(data, "keepalive_interval", cfg.Interval), keepAliveMinInterval, keepAliveMaxInterval)
	}
	locked := []string{}
	if v, ok := os.LookupEnv("KEEPALIVE_URL"); ok {
		cfg.URL = strings.TrimSpace(v)
		locked = append(locked, "keepalive_url")
	}
	if v, ok := os.LookupEnv("KEEPALIVE_INTERVAL"); ok {
		if i, err := strconv.Atoi(strings.TrimSpace(v)); err == nil {
			cfg.Interval = clampInt(i, keepAliveMinInterval, keepAliveMaxInterval)
		}
		locked = append(locked, "keepalive_interval")
	}
	cfg.EnvLocked = locked
	return cfg
}

func (app *App) updateKeepAliveSettings(body map[string]any) error {
	if _, hasURL := body["keepalive_url"]; !hasURL {
		if _, hasInterval := body["keepalive_interval"]; !hasInterval {
			return nil
		}
	}
	data := map[string]any{}
	if app.configStore != nil {
		_ = app.configStore.LoadInto(&data)
	}
	locked := map[string]bool{}
	for _, key := range app.keepaliveConfig().EnvLocked {
		locked[key] = true
	}
	if _, ok := body["keepalive_url"]; ok && !locked["keepalive_url"] {
		data["keepalive_url"] = stringValue(body, "keepalive_url", "")
	}
	if _, ok := body["keepalive_interval"]; ok && !locked["keepalive_interval"] {
		interval := intValue(body, "keepalive_interval", keepAliveDefaultInterval)
		if interval < keepAliveMinInterval || interval > keepAliveMaxInterval {
			return fmt.Errorf("保活间隔必须在 %d - %d 秒之间", keepAliveMinInterval, keepAliveMaxInterval)
		}
		data["keepalive_interval"] = interval
	}
	if app.configStore != nil {
		if err := app.configStore.Save(data); err != nil {
			return err
		}
	}
	if app.keepalive != nil {
		app.keepalive.Apply(app.keepaliveConfig())
	}
	return nil
}

// ---- migrated from handlers.go ----
type AuthContext struct {
	Token string
	User  map[string]any
}

func (app *App) routes() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /api", app.handleAPI)
	mux.HandleFunc("GET /healthz", app.handleHealth)
	mux.HandleFunc("GET /readyz", app.handleReady)
	mux.HandleFunc("GET /keepalive", app.handleKeepAlive)
	mux.HandleFunc("HEAD /keepalive", app.handleKeepAlive)

	mux.HandleFunc("POST /v1/chat/completions", app.handleChatCompletions)
	mux.HandleFunc("POST /chat/completions", app.handleChatCompletions)
	mux.HandleFunc("POST /v1/responses", app.handleResponses)
	mux.HandleFunc("POST /responses", app.handleResponses)
	mux.HandleFunc("GET /v1/models", app.handleListModels)
	mux.HandleFunc("GET /v1/models/{model_id}", app.handleGetModel)
	mux.HandleFunc("POST /v1/embeddings", app.handleEmbeddings)
	mux.HandleFunc("POST /embeddings", app.handleEmbeddings)
	mux.HandleFunc("POST /v1/images/generations", app.handleImages)
	mux.HandleFunc("POST /images/generations", app.handleImages)
	mux.HandleFunc("POST /v1/videos/generations", app.handleVideos)
	mux.HandleFunc("POST /videos/generations", app.handleVideos)
	mux.HandleFunc("POST /v1/files", app.handleUploadFile)
	mux.HandleFunc("POST /api/files/upload", app.handleUploadFile)
	mux.HandleFunc("DELETE /v1/files/{file_id}", app.handleDeleteFile)
	mux.HandleFunc("DELETE /api/files/{file_id}", app.handleDeleteFile)

	mux.HandleFunc("POST /anthropic/v1/messages", app.handleAnthropicMessages)
	mux.HandleFunc("POST /v1/messages", app.handleAnthropicMessages)
	mux.HandleFunc("POST /messages", app.handleAnthropicMessages)
	mux.HandleFunc("POST /anthropic/v1/messages/count_tokens", app.handleAnthropicCountTokens)
	mux.HandleFunc("POST /v1/messages/count_tokens", app.handleAnthropicCountTokens)
	mux.HandleFunc("POST /messages/count_tokens", app.handleAnthropicCountTokens)

	mux.HandleFunc("POST /v1beta/models/", app.handleGeminiRoute)
	mux.HandleFunc("POST /v1/models/", app.handleGeminiRoute)
	mux.HandleFunc("POST /models/", app.handleGeminiRoute)

	mux.HandleFunc("GET /api/admin/status", app.adminStatus)
	mux.HandleFunc("GET /api/admin/users", app.adminListUsers)
	mux.HandleFunc("POST /api/admin/users", app.adminCreateUser)
	mux.HandleFunc("GET /api/admin/accounts", app.adminListAccounts)
	mux.HandleFunc("POST /api/admin/accounts", app.adminAddAccount)
	mux.HandleFunc("POST /api/admin/verify", app.adminVerifyAll)
	mux.HandleFunc("POST /api/admin/accounts/{email}/activate", app.adminActivateAccount)
	mux.HandleFunc("POST /api/admin/accounts/{email}/verify", app.adminVerifyAccount)
	mux.HandleFunc("DELETE /api/admin/accounts/{email}", app.adminDeleteAccount)
	mux.HandleFunc("GET /api/admin/settings", app.adminGetSettings)
	mux.HandleFunc("PUT /api/admin/settings", app.adminUpdateSettings)
	mux.HandleFunc("GET /api/admin/keys", app.adminGetKeys)
	mux.HandleFunc("POST /api/admin/keys", app.adminCreateKey)
	mux.HandleFunc("DELETE /api/admin/keys/{key}", app.adminDeleteKey)
	mux.HandleFunc("GET /admin/dev/captures", app.adminGetCaptures)
	mux.HandleFunc("DELETE /admin/dev/captures", app.adminDeleteCaptures)
	mux.HandleFunc("GET /", app.handleSPA)

	return app.withRequestLogging(app.withCORS(mux))
}

func (app *App) handleGeminiRoute(w http.ResponseWriter, r *http.Request) {
	switch {
	case strings.HasSuffix(r.URL.Path, ":generateContent"):
		app.handleGeminiGenerate(w, r)
	case strings.HasSuffix(r.URL.Path, ":streamGenerateContent"):
		app.handleGeminiStream(w, r)
	default:
		http.NotFound(w, r)
	}
}

func (app *App) withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Credentials", "true")
		w.Header().Set("Access-Control-Allow-Headers", "*")
		w.Header().Set("Access-Control-Allow-Methods", "*")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (app *App) handleAPI(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "qwen2API Enterprise Gateway is running", "docs": "/docs", "version": Version})
}

func (app *App) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok"})
}

func (app *App) handleReady(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"status": "ready", "accounts": app.accounts.Status()})
}

func (app *App) handleKeepAlive(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodHead {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "version": Version})
}

func (app *App) handleSPA(w http.ResponseWriter, r *http.Request) {
	if strings.HasPrefix(r.URL.Path, "/api/") || strings.HasPrefix(r.URL.Path, "/v1/") || strings.HasPrefix(r.URL.Path, "/anthropic/") || strings.HasPrefix(r.URL.Path, "/models/") || strings.HasPrefix(r.URL.Path, "/messages") {
		http.NotFound(w, r)
		return
	}
	if _, err := os.Stat(app.settings.FrontendDist); err != nil {
		http.NotFound(w, r)
		return
	}
	target := filepath.Join(app.settings.FrontendDist, filepath.Clean("/"+r.URL.Path))
	if info, err := os.Stat(target); err == nil && !info.IsDir() {
		http.ServeFile(w, r, target)
		return
	}
	http.ServeFile(w, r, filepath.Join(app.settings.FrontendDist, "index.html"))
}

func (app *App) resolveAuth(w http.ResponseWriter, r *http.Request) (*AuthContext, bool) {
	token := extractAPIToken(r)
	if token == "" {
		writeError(w, http.StatusUnauthorized, "Invalid API Key")
		return nil, false
	}
	var users []map[string]any
	_ = app.usersStore.LoadInto(&users)
	var user map[string]any
	for _, candidate := range users {
		if stringValue(candidate, "id", "") == token {
			user = candidate
			break
		}
	}
	if len(app.apiKeys) > 0 && token != app.settings.AdminKey && !app.apiKeys[token] && user == nil {
		writeError(w, http.StatusUnauthorized, "Invalid API Key")
		return nil, false
	}
	if user != nil {
		quota := intValue(user, "quota", 0)
		used := intValue(user, "used_tokens", 0)
		if quota > 0 && used >= quota {
			writeError(w, http.StatusPaymentRequired, "Quota Exceeded")
			return nil, false
		}
	}
	return &AuthContext{Token: token, User: user}, true
}

func (app *App) verifyAdmin(w http.ResponseWriter, r *http.Request) (string, bool) {
	token := extractAPIToken(r)
	if token == "" {
		writeError(w, http.StatusUnauthorized, "Unauthorized")
		return "", false
	}
	if token != app.settings.AdminKey && !app.apiKeys[token] {
		writeError(w, http.StatusForbidden, "Forbidden: Admin Key Mismatch")
		return "", false
	}
	return token, true
}

func extractAPIToken(r *http.Request) string {
	auth := r.Header.Get("Authorization")
	if strings.HasPrefix(auth, "Bearer ") {
		return strings.TrimSpace(strings.TrimPrefix(auth, "Bearer "))
	}
	if token := strings.TrimSpace(r.Header.Get("x-api-key")); token != "" {
		return token
	}
	if token := strings.TrimSpace(r.URL.Query().Get("key")); token != "" {
		return token
	}
	return strings.TrimSpace(r.URL.Query().Get("api_key"))
}

func (app *App) runCompletion(ctx context.Context, req StandardRequest, preferredEmail string) (CompletionResult, error) {
	return app.runCompletionWithHooks(ctx, req, preferredEmail, nil)
}

func (app *App) runCompletionWithHooks(ctx context.Context, req StandardRequest, preferredEmail string, hooks *completionStreamHooks) (CompletionResult, error) {
	if req.BoundAccount != nil {
		preferredEmail = req.BoundAccount.Email
	}
	if preferredEmail == "" {
		preferredEmail = req.PreferredEmail
	}
	var (
		acc            *Account
		err            error
		releaseAccount bool
	)
	if req.BoundAccount != nil {
		acc = req.BoundAccount
		releaseAccount = true
	} else {
		acc, err = app.accounts.Acquire(ctx, preferredEmail)
		if err != nil {
			app.logWarn(ctx, "获取账号失败", "error", err)
			return CompletionResult{}, err
		}
		releaseAccount = true
	}
	if releaseAccount {
		defer app.accounts.Release(acc)
	}
	setRequestLogFields(ctx, "account", acc.Email)
	app.logInfo(ctx, "获取账号", "preferred_email", firstNonEmpty(preferredEmail, "-"))

	app.chatPool.RememberModel(req.ResolvedModel, req.ChatType)
	chatID, reused := app.chatPool.Take(ctx, acc.Email, req.ResolvedModel, req.ChatType)
	if !reused {
		chatID, err = app.client.CreateChat(ctx, qwenCreds(acc.Token, acc.Cookies), req.ResolvedModel, req.ChatType)
		if err != nil {
			app.classifyAccountError(acc, err)
			app.logWarn(ctx, "创建上游会话失败", "account", acc.Email, "error", err)
			return CompletionResult{}, err
		}
	}
	defer asyncDeleteChat(app.client, acc.Token, acc.Cookies, chatID)
	setRequestLogFields(ctx, "chat_id", chatID)
	app.logInfo(ctx, "创建上游会话", "chat_type", req.ChatType, "prewarmed", reused)

	payload := buildChatPayload(chatID, req.ResolvedModel, req.Prompt, req.ToolEnabled, req.UpstreamFiles, req.ChatType, nil, req.ThinkingEnabled, req.EnableSearch)
	result := CompletionResult{FinishReason: "stop"}
	start := time.Now()
	var sieve *toolcall.ToolSieve
	if req.ToolEnabled {
		sieve = toolcall.NewToolSieve(req.Tools)
		app.logInfo(ctx, "[Collect] tool filter enabled", "tools", strings.Join(req.ToolNames, ","))
	}
	streamContent := func(reasoning bool, text string) error {
		if text == "" {
			return nil
		}
		if reasoning {
			result.ReasoningText += text
			if hooks != nil && hooks.OnReasoningDelta != nil && !shouldBufferStreamTextDeltas(req) {
				return hooks.OnReasoningDelta(text)
			}
			return nil
		}
		result.AnswerText += text
		if hooks != nil && hooks.OnAnswerDelta != nil && !shouldBufferStreamTextDeltas(req) {
			return hooks.OnAnswerDelta(text)
		}
		return nil
	}
	err = app.client.StreamChat(ctx, qwenCreds(acc.Token, acc.Cookies), chatID, payload, func(evt UpstreamEvent) error {
		result.Events = append(result.Events, evt)
		if evt.Type != "delta" || evt.Content == "" {
			return nil
		}
		reasoning := strings.Contains(evt.Phase, "thinking") || strings.Contains(evt.Phase, "reasoning")
		if sieve == nil {
			return streamContent(reasoning, evt.Content)
		}
		for _, sieveEvt := range sieve.ProcessChunk(evt.Content) {
			switch sieveEvt.Type {
			case "content":
				if err := streamContent(reasoning, sieveEvt.Text); err != nil {
					return err
				}
			case "tool_calls":
				if len(sieveEvt.Calls) == 0 {
					continue
				}
				stage := "tool_sieve_stream"
				if reasoning {
					stage = "tool_sieve_reasoning_stream"
				}
				captured, err := app.captureSieveToolCalls(ctx, req, &result, sieveEvt.Calls, stage, func(calls []ParsedToolCall) error {
					if hooks != nil && hooks.OnToolCalls != nil {
						return hooks.OnToolCalls(calls)
					}
					return nil
				})
				if err != nil {
					return err
				}
				if !captured {
					// A blocked repeat/invalid call may be followed by a valid
					// replacement in the same upstream response. Suppress only
					// that call and keep collecting.
					continue
				}
				return errToolSieveDetected
			}
		}
		if reasoning {
			if blocked := extractBlockedToolNames(result.ReasoningText, req.ToolNames); len(blocked) > 0 {
				result.FinishReason = "blocked_tool_name:" + blocked[0]
				app.logWarn(ctx, "[Collect] blocked contaminated reasoning before client stream", "preview", promptTail(result.ReasoningText, 120), "blocked_tool", blocked[0])
				suppressBlockedToolNameOutput(&result, req.ToolNames)
				return errToolSieveDetected
			}
		} else {
			if blocked := extractBlockedToolNames(result.AnswerText, req.ToolNames); len(blocked) > 0 {
				result.FinishReason = "blocked_tool_name:" + blocked[0]
				app.logWarn(ctx, "[Collect] blocked contaminated output before client stream", "preview", promptTail(result.AnswerText, 120), "blocked_tool", blocked[0])
				suppressBlockedToolNameOutput(&result, req.ToolNames)
				return errToolSieveDetected
			}
		}
		return nil
	})
	if err != nil && !errors.Is(err, errToolSieveDetected) {
		app.classifyAccountError(acc, err)
		app.logWarn(ctx, "上游流式失败", "error", err, "events", len(result.Events), "duration_ms", time.Since(start).Milliseconds())
		return result, err
	}
	app.accounts.MarkSuccess(acc)
	app.logInfo(ctx, "上游流式完成", "events", len(result.Events), "answer_len", len(result.AnswerText), "reasoning_len", len(result.ReasoningText), "duration_ms", time.Since(start).Milliseconds())
	if result.AnswerText != "" || result.ReasoningText != "" {
		app.logInfo(ctx, "上游回复摘要", "answer_tail", promptTail(result.AnswerText, 600), "reasoning_tail", promptTail(result.ReasoningText, 300))
	}
	if req.ToolEnabled {
		if len(result.ToolCalls) == 0 && sieve != nil {
			for _, sieveEvt := range sieve.Flush() {
				switch sieveEvt.Type {
				case "content":
					if strings.TrimSpace(sieveEvt.Text) != "" {
						_ = streamContent(false, sieveEvt.Text)
					}
				case "tool_calls":
					if len(sieveEvt.Calls) == 0 {
						continue
					}
					captured, _ := app.captureSieveToolCalls(ctx, req, &result, sieveEvt.Calls, "tool_sieve_flush", nil)
					if captured {
						break
					}
				}
			}
		}
		if len(result.ToolCalls) == 0 && isBlockedToolNameOutput(result, req.ToolNames) {
			blocked := firstBlockedToolName(result, req.ToolNames)
			app.logWarn(ctx, "[Collect] recovering blocked tool-name contamination", "blocked_tool", firstNonEmpty(blocked, "-"), "preview", promptTail(result.AnswerText, 180))
			result = app.recoverBlockedToolNameOutput(ctx, acc, req, result)
			if len(result.ToolCalls) > 0 {
				app.logParsedToolCalls(ctx, "ToolCall", "final:"+collectFinalizeReason(result, result.ToolCalls), result.ToolCalls)
			} else if isBlockedToolNameOutput(result, req.ToolNames) {
				app.logWarn(ctx, "[Collect] unrecovered blocked tool-name contamination suppressed", "blocked_tool", firstNonEmpty(firstBlockedToolName(result, req.ToolNames), "-"), "preview", promptTail(result.AnswerText, 180))
				suppressBlockedToolNameOutput(&result, req.ToolNames)
				if blocked != "" {
					result.FinishReason = "blocked_tool_name:" + blocked
				}
			}
		}
		if len(result.ToolCalls) == 0 {
			parsed := parseToolCalls(toolParseText(result), req.Tools)
			if len(parsed) > 0 {
				app.captureDetectedToolCalls(ctx, req, &result, parsed, "final_text_parse")
				app.logInfo(ctx, "[Collect] final text parser detected tool calls", "tools", parsedToolNames(result.ToolCalls), "cleaned_text_len", len(toolParseText(result)))
			}
		}
		if len(result.ToolCalls) == 0 && isRepeatedToolCallBlockedResult(result) {
			app.logWarn(ctx, "[Collect] recovering repeated tool call loop", "tool", repeatedToolCallName(req, result), "history_count", req.RepeatedToolCount)
			result = app.recoverRepeatedToolCall(ctx, acc, req, result)
			if len(result.ToolCalls) > 0 {
				app.logParsedToolCalls(ctx, "ToolCall", "final:"+collectFinalizeReason(result, result.ToolCalls), result.ToolCalls)
			} else if isRepeatedToolCallBlockedResult(result) {
				app.logWarn(ctx, "[Collect] unrecovered repeated tool call loop replaced with safe retry text", "tool", repeatedToolCallName(req, result), "history_count", req.RepeatedToolCount)
				result.AnswerText = repeatedToolCallFallback(req, result)
				result.ReasoningText = ""
			}
		}
		if len(result.ToolCalls) > 0 {
			app.logParsedToolCalls(ctx, "ToolCall", "final:"+collectFinalizeReason(result, result.ToolCalls), result.ToolCalls)
		} else if isInvalidToolArgsResult(result) {
			app.logWarn(ctx, "[Collect] recovering invalid tool arguments", "tool", invalidToolArgsName(result), "preview", promptTail(toolParseText(result), 180))
			result = app.recoverInvalidToolCallArgs(ctx, acc, req, result)
			if len(result.ToolCalls) > 0 {
				app.logParsedToolCalls(ctx, "ToolCall", "final:"+collectFinalizeReason(result, result.ToolCalls), result.ToolCalls)
			} else if isInvalidToolArgsResult(result) {
				app.logWarn(ctx, "[Collect] unrecovered invalid tool arguments replaced with safe retry text", "tool", invalidToolArgsName(result), "preview", promptTail(toolParseText(result), 180))
				result.AnswerText = invalidToolArgsFallback(result)
				result.ReasoningText = ""
			}
		} else if hasTextualToolMarker(toolParseText(result)) {
			app.logWarn(ctx, "[Collect] blocked unparsed textual tool markup before client output", "attempted", promptTail(toolParseText(result), 240), "len", len(toolParseText(result)), "reason", "unparsed_textual_tool_marker")
			result = app.recoverUnparsedToolMarkup(ctx, acc, req, result)
			if len(result.ToolCalls) > 0 {
				app.logParsedToolCalls(ctx, "ToolCall", "final:"+collectFinalizeReason(result, result.ToolCalls), result.ToolCalls)
			} else if hasTextualToolMarker(toolParseText(result)) {
				app.logWarn(ctx, "[Collect] unrecovered textual tool markup replaced with safe retry text", "attempted", promptTail(toolParseText(result), 240), "len", len(toolParseText(result)))
				result.AnswerText = invalidToolMarkupFallback(req, result)
				result.ReasoningText = ""
				result.FinishReason = "invalid_tool_args"
			} else if result.AnswerText == "" && result.ReasoningText == "" {
				result.AnswerText = invalidToolMarkupFallback(req, result)
				result.FinishReason = "invalid_tool_args"
			}
		} else if shouldForceToolContinuation(req, result) {
			app.logWarn(ctx, "[Collect] recovering missing tool continuation after tool result", "preview", promptTail(toolParseText(result), 180), "answer_len", len(result.AnswerText), "reasoning_len", len(result.ReasoningText))
			result = app.recoverMissingToolContinuation(ctx, acc, req, result)
			if len(result.ToolCalls) > 0 {
				app.logParsedToolCalls(ctx, "ToolCall", "final:"+collectFinalizeReason(result, result.ToolCalls), result.ToolCalls)
			} else if shouldForceToolContinuation(req, result) {
				app.logWarn(ctx, "[Collect] unrecovered missing tool continuation replaced with safe retry text", "preview", promptTail(toolParseText(result), 180))
				result.AnswerText = missingToolContinuationFallback()
				result.ReasoningText = ""
				result.FinishReason = "missing_tool_continuation"
			}
		} else if shouldRecoverMissingInitialToolCall(req, result) {
			app.logWarn(ctx, "[Collect] recovering missing initial tool call", "preview", promptTail(toolParseText(result), 180), "answer_len", len(result.AnswerText), "reasoning_len", len(result.ReasoningText))
			result = app.recoverMissingInitialToolCall(ctx, acc, req, result)
			if len(result.ToolCalls) > 0 {
				app.logParsedToolCalls(ctx, "ToolCall", "final:"+collectFinalizeReason(result, result.ToolCalls), result.ToolCalls)
			} else if shouldRecoverMissingInitialToolCall(req, result) {
				app.logWarn(ctx, "[Collect] unrecovered missing initial tool call replaced with honest retry text", "preview", promptTail(toolParseText(result), 180))
				result.AnswerText = missingInitialToolCallFallback()
				result.ReasoningText = ""
				result.FinishReason = "missing_initial_tool_call"
			}
		} else if result.AnswerText == "" && result.ReasoningText == "" && !strings.HasPrefix(result.FinishReason, "blocked_tool_name:") {
			result.FinishReason = "empty"
		}
		if len(result.ToolCalls) == 0 && isBlockedToolNameOutput(result, req.ToolNames) {
			if strings.TrimSpace(result.AnswerText) != "" || strings.TrimSpace(result.ReasoningText) != "" {
				app.logWarn(ctx, "[Collect] suppressed blocked tool-name text before finalize", "blocked_tool", firstNonEmpty(firstBlockedToolName(result, req.ToolNames), "-"), "preview", promptTail(toolParseText(result), 180))
			}
			suppressBlockedToolNameOutput(&result, req.ToolNames)
		}
		app.logInfo(ctx, "[Collect] finalize", "reason", collectFinalizeReason(result, result.ToolCalls), "chat_id", chatID, "tool_calls", len(result.ToolCalls), "answer_chars", len(result.AnswerText), "reasoning_chars", len(result.ReasoningText), "finish_reason", firstNonEmpty(result.FinishReason, "stop"))
	}
	return result, nil
}

func (app *App) captureDetectedToolCalls(ctx context.Context, req StandardRequest, result *CompletionResult, calls []ParsedToolCall, stage string) bool {
	if result == nil || len(calls) == 0 {
		return false
	}
	merged := dedupeToolCalls(append(result.ToolCalls, calls...))
	kept, blocked := filterRepeatedToolCalls(req, merged)
	if len(blocked) > 0 {
		app.logWarn(ctx, "[Collect] blocked repeated tool call before client delivery",
			"stage", stage,
			"tool", blocked[0].Name,
			"history_count", req.RepeatedToolCount,
			"blocked_count", len(blocked),
			"input", toolInputPreview(blocked[0].Input, 240),
		)
	}
	kept, invalid := filterInvalidToolCalls(kept)
	invalidReason := ""
	if len(invalid) > 0 {
		invalidReason = invalidToolCallReason(invalid[0])
	}
	promptInvalidFragment := ""
	if promptKept, promptInvalid := filterPromptDisallowedToolCalls(req, kept); len(promptInvalid) > 0 {
		if invalidReason == "" {
			invalidReason = "prompt_disallowed"
		}
		promptInvalidFragment = promptDisallowedToolCallFragment(req.Prompt, promptInvalid[0].Name)
		kept = promptKept
		invalid = append(invalid, promptInvalid...)
	}
	if orderedKept, orderedInvalid := filterOutOfOrderToolCalls(req, kept); len(orderedInvalid) > 0 {
		if invalidReason == "" {
			invalidReason = "out_of_order_round"
		}
		kept = orderedKept
		invalid = append(invalid, orderedInvalid...)
	}
	if len(invalid) > 0 {
		if invalidReason == "" {
			invalidReason = "unknown"
		}
		app.logWarn(ctx, "[Collect] blocked invalid tool arguments before client delivery",
			"stage", stage,
			"tool", invalid[0].Name,
			"invalid_count", len(invalid),
			"invalid_reason", invalidReason,
			"prompt_fragment", promptInvalidFragment,
			"input", toolInputPreview(invalid[0].Input, 900),
		)
		result.InvalidToolCallSignatures = append(result.InvalidToolCallSignatures, parsedToolCallSignatures(invalid)...)
		result.InvalidToolCallReasons = append(result.InvalidToolCallReasons, invalidReason)
	}
	if len(kept) == 0 {
		result.ToolCalls = nil
		if len(invalid) > 0 {
			result.FinishReason = "invalid_tool_args:" + firstNonEmpty(invalid[0].Name, "unknown")
		} else {
			result.FinishReason = "repeated_tool_call:" + firstNonEmpty(repeatedToolCallName(req, *result), firstRepeatedToolName(blocked), "unknown")
		}
		return false
	}
	result.ToolCalls = kept
	result.FinishReason = "tool_calls"
	app.logParsedToolCalls(ctx, "ToolCall", stage, result.ToolCalls)
	return true
}

func (app *App) captureSieveToolCalls(ctx context.Context, req StandardRequest, result *CompletionResult, calls []ParsedToolCall, stage string, onCaptured func([]ParsedToolCall) error) (bool, error) {
	if result == nil || len(calls) == 0 {
		return false, nil
	}
	if !app.captureDetectedToolCalls(ctx, req, result, calls, stage) {
		app.logInfo(ctx, "[Collect] suppressed tool call and continued stream",
			"stage", stage,
			"finish_reason", firstNonEmpty(result.FinishReason, "unknown"),
		)
		return false, nil
	}
	app.logInfo(ctx, "[Collect] Tool Sieve detected deliverable tool calls", "stage", stage, "tools", parsedToolNames(result.ToolCalls))
	if onCaptured != nil {
		if err := onCaptured(result.ToolCalls); err != nil {
			return true, err
		}
	}
	return true, nil
}

func filterInvalidToolCalls(calls []ParsedToolCall) ([]ParsedToolCall, []ParsedToolCall) {
	if len(calls) == 0 {
		return calls, nil
	}
	kept := make([]ParsedToolCall, 0, len(calls))
	invalid := []ParsedToolCall{}
	for _, call := range calls {
		if invalidToolCallReason(call) != "" {
			invalid = append(invalid, call)
			continue
		}
		kept = append(kept, call)
	}
	return kept, invalid
}

func filterPromptDisallowedToolCalls(req StandardRequest, calls []ParsedToolCall) ([]ParsedToolCall, []ParsedToolCall) {
	if len(calls) == 0 || strings.TrimSpace(req.Prompt) == "" {
		return calls, nil
	}
	kept := make([]ParsedToolCall, 0, len(calls))
	invalid := []ParsedToolCall{}
	for _, call := range calls {
		if promptDisallowsToolCall(req.Prompt, call.Name) {
			invalid = append(invalid, call)
			continue
		}
		kept = append(kept, call)
	}
	return kept, invalid
}

func promptDisallowsToolCall(prompt, toolName string) bool {
	return promptDisallowedToolCallFragment(prompt, toolName) != ""
}

func promptDisallowedToolCallFragment(prompt, toolName string) string {
	prompt = strings.TrimSpace(prompt)
	if prompt == "" || strings.TrimSpace(toolName) == "" {
		return ""
	}
	aliases := promptDisallowedToolAliasSpecs(toolName)
	if len(aliases) == 0 {
		return ""
	}
	for _, fragment := range promptDisallowFragments(prompt) {
		if !promptDisallowTargetsTool(fragment, aliases) {
			continue
		}
		if !promptDisallowFragmentAppliesNow(prompt, fragment) {
			continue
		}
		return fragment
	}
	return ""
}

type promptToolAlias struct {
	Raw string
	Key string
}

func promptDisallowedToolAliases(toolName string) []string {
	specs := promptDisallowedToolAliasSpecs(toolName)
	out := make([]string, 0, len(specs))
	for _, spec := range specs {
		out = append(out, spec.Key)
	}
	return out
}

func promptDisallowedToolAliasSpecs(toolName string) []promptToolAlias {
	seen := map[string]promptToolAlias{}
	add := func(value string) {
		raw := strings.TrimSpace(value)
		key := normalizeToolNameKey(value)
		if key != "" {
			if _, ok := seen[key]; !ok {
				seen[key] = promptToolAlias{Raw: raw, Key: key}
			}
		}
	}
	add(toolName)
	add(strings.ReplaceAll(toolName, "_", " "))
	add(strings.ReplaceAll(toolName, "-", " "))
	switch normalizeToolNameKey(toolName) {
	case "delegatetask":
		add("delegate_task")
		add("delegate task")
		add("delegate")
		add("agent")
		add("subagent")
	case "agent", "subagent":
		add("delegate_task")
		add("delegate")
		add("agent")
		add("subagent")
	}
	keys := make([]string, 0, len(seen))
	for key := range seen {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	out := make([]promptToolAlias, 0, len(keys))
	for _, key := range keys {
		out = append(out, seen[key])
	}
	return out
}

func promptDisallowFragments(prompt string) []string {
	fragments := []string{}
	for _, line := range strings.Split(prompt, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		for _, piece := range regexp.MustCompile(`[。；;]+`).Split(line, -1) {
			piece = strings.TrimSpace(piece)
			if piece != "" {
				fragments = append(fragments, piece)
			}
		}
	}
	return fragments
}

func hasPromptDisallowCallVerb(fragment string) bool {
	fragment = strings.TrimSpace(fragment)
	if fragment == "" {
		return false
	}
	chinese := regexp.MustCompile(`(?is)(不要再|不要|别|请勿|勿|禁止|严禁|不得|不能|不允许|不可|不再)[^。\n；;]{0,40}(调用|使用|执行|运行|发起|触发)`)
	if chinese.MatchString(fragment) {
		return true
	}
	english := regexp.MustCompile(`(?is)\b(do\s+not|don't|dont|never|must\s+not|should\s+not|forbid(?:den)?(?:\s+to)?|not\s+allowed\s+to)\b(?:\W+\w+){0,6}\W+(call|use|invoke|run|execute|trigger|launch)\b`)
	return english.MatchString(fragment)
}

func promptDisallowTargetsTool(fragment string, aliases []promptToolAlias) bool {
	fragment = strings.TrimSpace(fragment)
	if fragment == "" {
		return false
	}
	if !hasPromptDisallowCallVerb(fragment) {
		return false
	}
	res := []*regexp.Regexp{
		regexp.MustCompile(`(?is)(不要再|不要|别|请勿|勿|禁止|严禁|不得|不能|不允许|不可|不再)[^。\n；;]{0,40}(调用|使用|执行|运行|发起|触发)`),
		regexp.MustCompile(`(?is)\b(do\s+not|don't|dont|never|must\s+not|should\s+not|forbid(?:den)?(?:\s+to)?|not\s+allowed\s+to)\b(?:\W+\w+){0,6}\W+(call|use|invoke|run|execute|trigger|launch)\b`),
	}
	for _, re := range res {
		for _, loc := range re.FindAllStringIndex(fragment, -1) {
			if len(loc) != 2 {
				continue
			}
			if promptFragmentContainsToolAlias(promptDisallowDirectiveScope(fragment, loc), aliases) {
				return true
			}
		}
	}
	return false
}

func promptDisallowDirectiveScope(fragment string, loc []int) string {
	if len(loc) != 2 {
		return fragment
	}
	start, end := loc[0], loc[1]
	if start < 0 || start > len(fragment) || end < start || end > len(fragment) {
		return fragment
	}
	tail := fragment[end:]
	if boundary := strings.IndexAny(tail, ".!?。；;\n"); boundary >= 0 {
		return promptDisallowDirectTargetScope(fragment[start:end+boundary], end-start)
	}
	return promptDisallowDirectTargetScope(fragment[start:], end-start)
}

func promptDisallowDirectTargetScope(scope string, verbEnd int) string {
	if verbEnd < 0 || verbEnd > len(scope) {
		return scope
	}
	tail := scope[verbEnd:]
	cutAt := len(tail)
	cutters := []*regexp.Regexp{
		regexp.MustCompile(`(?is)\b(?:as\s+(?:a\s+)?(?:substitute|replacement|fallback)\s+for|instead\s+of|rather\s+than|when|if|unless|for)\b`),
		regexp.MustCompile(`(?is)(作为|当作|充当|代替|替代|而不是|如果|若|如需|请使用|改用)`),
	}
	for _, re := range cutters {
		if loc := re.FindStringIndex(tail); len(loc) == 2 && loc[0] < cutAt {
			cutAt = loc[0]
		}
	}
	return scope[:verbEnd+cutAt]
}

func promptFragmentContainsToolAlias(fragment string, aliases []promptToolAlias) bool {
	fragment = strings.TrimSpace(fragment)
	if fragment == "" {
		return false
	}
	quoted := promptQuotedToolTokens(fragment)
	if len(quoted) > 0 {
		for _, token := range quoted {
			if promptToolTokenMatchesAlias(token, aliases) {
				return true
			}
		}
		return false
	}
	for _, token := range regexp.MustCompile(`[A-Za-z0-9_.:-]+`).FindAllString(fragment, -1) {
		if promptToolTokenMatchesAlias(token, aliases) {
			return true
		}
	}
	return false
}

func promptQuotedToolTokens(fragment string) []string {
	out := []string{}
	for _, match := range regexp.MustCompile("`([^`]+)`").FindAllStringSubmatch(fragment, -1) {
		if len(match) < 2 {
			continue
		}
		for _, token := range regexp.MustCompile(`[、,，/\s]+`).Split(match[1], -1) {
			token = strings.TrimSpace(token)
			if token != "" {
				out = append(out, token)
			}
		}
	}
	return out
}

func promptToolTokenMatchesAlias(token string, aliases []promptToolAlias) bool {
	token = strings.TrimSpace(token)
	if token == "" {
		return false
	}
	tokenKey := normalizeToolNameKey(token)
	if tokenKey == "" {
		return false
	}
	for _, alias := range aliases {
		if alias.Key == "" || tokenKey != alias.Key {
			continue
		}
		if token == alias.Raw {
			return true
		}
		if token == strings.ToLower(token) && alias.Raw == strings.ToLower(alias.Raw) {
			return true
		}
	}
	return false
}

func promptDisallowFragmentAppliesNow(prompt, fragment string) bool {
	rounds := extractPlainRoundMentions(fragment)
	if len(rounds) == 0 || promptDisallowFragmentLooksGlobal(fragment) {
		return true
	}
	next := nextUnmetPlannedRound(prompt)
	return next > 0 && intSliceContains(rounds, next)
}

func promptDisallowFragmentLooksGlobal(fragment string) bool {
	lower := strings.ToLower(fragment)
	for _, needle := range []string{
		"全程",
		"任何时候",
		"所有轮",
		"全部轮",
		"其它工具",
		"其他工具",
		"只允许调用",
		"只允许使用",
		"only allowed",
		"all rounds",
		"throughout",
		"globally",
		"never",
	} {
		if strings.Contains(lower, strings.ToLower(needle)) {
			return true
		}
	}
	return false
}

func nextUnmetPlannedRound(prompt string) int {
	if round := taskMemoryEarliestUnmetRound(prompt); round > 0 {
		return round
	}
	for _, round := range extractPlainRoundMentions(prompt) {
		if plannedRoundInPrompt(prompt, round) && !recordedRoundInPrompt(prompt, round) {
			return round
		}
	}
	return 0
}

func taskMemoryEarliestUnmetRound(prompt string) int {
	for _, match := range regexp.MustCompile(`(?im)^\s*EARLIEST PLANNED ROUND WITHOUT OBSERVED JSON RECORD:\s*R(\d{3,4})\s*$`).FindAllStringSubmatch(normalizedRoundScanText(prompt), -1) {
		if len(match) < 2 {
			continue
		}
		round, err := strconv.Atoi(match[1])
		if err == nil && round > 0 {
			return round
		}
	}
	return 0
}

func extractPlainRoundMentions(text string) []int {
	out := []int{}
	seen := map[int]bool{}
	for _, match := range regexp.MustCompile(`(?i)\bR(\d{3,4})\b`).FindAllStringSubmatch(normalizedRoundScanText(text), -1) {
		if len(match) < 2 {
			continue
		}
		n, err := strconv.Atoi(match[1])
		if err != nil || n <= 0 || seen[n] {
			continue
		}
		seen[n] = true
		out = append(out, n)
	}
	sort.Ints(out)
	return out
}

func filterOutOfOrderToolCalls(req StandardRequest, calls []ParsedToolCall) ([]ParsedToolCall, []ParsedToolCall) {
	if len(calls) == 0 || strings.TrimSpace(req.Prompt) == "" {
		return calls, nil
	}
	kept := make([]ParsedToolCall, 0, len(calls))
	invalid := []ParsedToolCall{}
	for _, call := range calls {
		if toolCallSkipsOrderedRound(req.Prompt, call) {
			invalid = append(invalid, call)
			continue
		}
		kept = append(kept, call)
	}
	return kept, invalid
}

func toolCallSkipsOrderedRound(prompt string, call ParsedToolCall) bool {
	rounds := toolCallRoundRecords(call)
	if len(rounds) == 0 {
		return false
	}
	for _, round := range rounds {
		if missingPriorPlannedRound(prompt, rounds, round) > 0 {
			return true
		}
	}
	return false
}

func missingPriorPlannedRound(prompt string, sameCallRounds []int, attemptedRound int) int {
	if attemptedRound <= 1 {
		return 0
	}
	if earliest := taskMemoryEarliestUnmetRound(prompt); earliest > 0 {
		if attemptedRound <= earliest || intSliceContains(sameCallRounds, earliest) {
			return 0
		}
		return earliest
	}
	for prev := 1; prev < attemptedRound; prev++ {
		if plannedRoundInPrompt(prompt, prev) && !recordedRoundInPrompt(prompt, prev) && !intSliceContains(sameCallRounds, prev) {
			return prev
		}
	}
	return 0
}

func invalidToolArgsRecoveryGuidance(req StandardRequest, result CompletionResult) string {
	if !isInvalidToolArgsResult(result) || strings.TrimSpace(req.Prompt) == "" {
		return ""
	}
	if disallowed := disallowedInvalidToolName(req, result); disallowed != "" {
		return "DISALLOWED TOOL RECOVERY: The blocked call used " + disallowed + ", but the current task instructions explicitly forbid calling that tool. Do not call it again. If the step only needs to record that capability as unavailable or not planned, use an allowed direct tool such as terminal/write_file to write the required record, keeping any requested JSON field value distinct from the actual tool call name."
	}
	calls := parseToolCalls(toolParseText(result), req.Tools)
	missing, attempted := earliestMissingRoundBeforeAttempt(req.Prompt, calls)
	if missing <= 0 || attempted <= 0 {
		return ""
	}
	return fmt.Sprintf(
		"ORDERED WORKFLOW RECOVERY: The blocked call attempted a later round R%03d, but planned round R%03d has no observed JSON progress record in the current prompt/history. Treat R%03d as the next unmet unit of work. If the latest tool result already completed R%03d's primary action, issue only the required bookkeeping/recording call for R%03d; otherwise execute R%03d's required action and record it. Do not mention, record, or execute later rounds in this retry.",
		attempted,
		missing,
		missing,
		missing,
		missing,
		missing,
	)
}

func disallowedInvalidToolName(req StandardRequest, result CompletionResult) string {
	name := invalidToolArgsName(result)
	if name != "" && promptDisallowsToolCall(req.Prompt, name) {
		return name
	}
	for _, call := range parseToolCalls(toolParseText(result), req.Tools) {
		if promptDisallowsToolCall(req.Prompt, call.Name) {
			return call.Name
		}
	}
	return ""
}

func earliestMissingRoundBeforeAttempt(prompt string, calls []ParsedToolCall) (int, int) {
	missing := 0
	attempted := 0
	for _, call := range calls {
		rounds := toolCallRoundRecords(call)
		for _, round := range rounds {
			currentMissing := missingPriorPlannedRound(prompt, rounds, round)
			if currentMissing <= 0 {
				continue
			}
			if missing == 0 || currentMissing < missing || (currentMissing == missing && (attempted == 0 || round < attempted)) {
				missing = currentMissing
				attempted = round
			}
		}
	}
	return missing, attempted
}

func toolCallRoundRecords(call ParsedToolCall) []int {
	text := normalizedRoundScanText(argTextValue(call.Input))
	return extractProgressRoundRecords(text)
}

func extractProgressRoundRecords(text string) []int {
	out := []int{}
	seen := map[int]bool{}
	for _, re := range []*regexp.Regexp{roundJSONRecordRe, roundJSONRecordAltRe} {
		for _, match := range re.FindAllStringSubmatch(text, -1) {
			if len(match) < 2 {
				continue
			}
			n, err := strconv.Atoi(match[1])
			if err != nil || n <= 0 || seen[n] {
				continue
			}
			seen[n] = true
			out = append(out, n)
		}
	}
	sort.Ints(out)
	return out
}

func plannedRoundInPrompt(prompt string, round int) bool {
	if round <= 0 {
		return false
	}
	return strings.Contains(normalizedRoundScanText(prompt), fmt.Sprintf("R%03d", round))
}

func recordedRoundInPrompt(prompt string, round int) bool {
	if round <= 0 {
		return false
	}
	for _, recorded := range extractProgressRoundRecords(normalizedRoundScanText(prompt)) {
		if recorded == round {
			return true
		}
	}
	return false
}

func normalizedRoundScanText(text string) string {
	return strings.ReplaceAll(text, "\\", "")
}

func intSliceContains(values []int, want int) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

func invalidToolCallSemantics(call ParsedToolCall) bool {
	return invalidToolCallReason(call) != ""
}

func invalidToolCallReason(call ParsedToolCall) string {
	args, ok := call.Input.(map[string]any)
	if !ok {
		return ""
	}
	if hasContaminatedTargetArg(args) {
		return "contaminated_target_arg"
	}
	name := normalizeToolNameKey(call.Name)
	switch name {
	case "patch", "edit", "multiedit", "notebookedit", "applypatch":
		if !hasAnyNonEmptyArg(args, "file", "path", "file_path", "filepath", "filename", "target", "target_file", "targetfile") {
			return "missing_patch_target"
		}
		if !hasAnyNonEmptyArg(args, "patch", "diff", "content", "text", "body", "old_string", "oldstring", "new_string", "newstring", "replacement", "edits", "changes", "operations", "hunks") {
			return "missing_patch_payload"
		}
	case "write", "writefile", "write_file", "createfile":
		if !hasAnyNonEmptyArg(args, "file", "path", "file_path", "filepath", "filename", "target", "target_file", "targetfile") {
			return "missing_write_target"
		}
		if !hasAnyFileContentArg(args, "content", "text", "body", "data", "value", "contents", "file_content", "filecontent") {
			return "missing_write_content"
		}
	case "read", "readfile", "read_file", "openfile":
		if !hasAnyNonEmptyArg(args, "file", "path", "file_path", "filepath", "filename", "target", "target_file", "targetfile") {
			return "missing_read_target"
		}
	case "bash", "powershell", "terminal", "shell", "shellrun", "execute", "execute_code":
		if !hasAnyNonEmptyArg(args, "command", "cmd", "script", "code", "input") {
			return "missing_execution_command"
		}
		if hasContaminatedExecutionArg(args) {
			return "contaminated_execution_arg"
		}
	case "process":
		if !hasAnyNonEmptyArg(args, "command", "cmd", "script", "code", "input", "action", "pid", "process_id", "processid", "name") {
			return "missing_process_action"
		}
	default:
		return ""
	}
	return ""
}

func hasContaminatedTargetArg(args map[string]any) bool {
	if len(args) == 0 {
		return false
	}
	targetKeys := map[string]bool{}
	for _, name := range []string{"file", "path", "file_path", "filepath", "filename", "target", "target_file", "targetfile"} {
		targetKeys[normalizeToolNameKey(name)] = true
	}
	for key, value := range args {
		if !targetKeys[normalizeToolNameKey(key)] {
			continue
		}
		if targetArgLooksContaminated(argTextValue(value)) {
			return true
		}
	}
	return false
}

func targetArgLooksContaminated(text string) bool {
	text = strings.TrimSpace(text)
	if text == "" {
		return false
	}
	if blockedToolNameTextRe.MatchString(text) || hasTextualToolMarker(text) {
		return true
	}
	if strings.ContainsAny(text, "`\r\n") {
		return true
	}
	lower := strings.ToLower(text)
	for _, needle := range []string{
		"unavailable:",
		"according to task memory",
		"next step",
		"use `",
		"call `",
		"根据任务记忆",
		"下一步",
		"本轮",
		"再用",
		"追加",
	} {
		if strings.Contains(lower, strings.ToLower(needle)) {
			return true
		}
	}
	return false
}

func hasContaminatedExecutionArg(args map[string]any) bool {
	if len(args) == 0 {
		return false
	}
	execKeys := map[string]bool{}
	for _, name := range []string{"command", "cmd", "script", "code", "input"} {
		execKeys[normalizeToolNameKey(name)] = true
	}
	for key, value := range args {
		if !execKeys[normalizeToolNameKey(key)] {
			continue
		}
		text := argTextValue(value)
		if hasTextualToolMarker(text) || blockedToolNameTextRe.MatchString(text) {
			return true
		}
	}
	return false
}

func argTextValue(value any) string {
	switch v := value.(type) {
	case nil:
		return ""
	case string:
		return v
	case []any:
		parts := make([]string, 0, len(v))
		for _, item := range v {
			if text := argTextValue(item); text != "" {
				parts = append(parts, text)
			}
		}
		return strings.Join(parts, "\n")
	case map[string]any:
		parts := make([]string, 0, len(v))
		for _, item := range v {
			if text := argTextValue(item); text != "" {
				parts = append(parts, text)
			}
		}
		return strings.Join(parts, "\n")
	default:
		return fmt.Sprint(v)
	}
}

func normalizeToolNameKey(name string) string {
	return regexp.MustCompile(`[^a-z0-9]+`).ReplaceAllString(strings.ToLower(strings.TrimSpace(name)), "")
}

func hasAnyNonEmptyArg(args map[string]any, names ...string) bool {
	if len(args) == 0 {
		return false
	}
	aliases := map[string]bool{}
	for _, name := range names {
		aliases[normalizeToolNameKey(name)] = true
	}
	for key, value := range args {
		if !aliases[normalizeToolNameKey(key)] {
			continue
		}
		if argValuePresent(value) {
			return true
		}
	}
	return false
}

func hasAnyPresentArg(args map[string]any, names ...string) bool {
	if len(args) == 0 {
		return false
	}
	aliases := map[string]bool{}
	for _, name := range names {
		aliases[normalizeToolNameKey(name)] = true
	}
	for key, value := range args {
		if aliases[normalizeToolNameKey(key)] && value != nil {
			return true
		}
	}
	return false
}

func hasAnyFileContentArg(args map[string]any, names ...string) bool {
	if len(args) == 0 {
		return false
	}
	aliases := map[string]bool{}
	for _, name := range names {
		aliases[normalizeToolNameKey(name)] = true
	}
	for key, value := range args {
		if !aliases[normalizeToolNameKey(key)] {
			continue
		}
		switch v := value.(type) {
		case nil:
			continue
		case string:
			return true
		case []any:
			if len(v) > 0 {
				return true
			}
		case map[string]any:
			if len(v) > 0 {
				return true
			}
		default:
			return true
		}
	}
	return false
}

func argValuePresent(value any) bool {
	switch v := value.(type) {
	case nil:
		return false
	case string:
		return strings.TrimSpace(v) != ""
	case []any:
		return len(v) > 0
	case map[string]any:
		return len(v) > 0
	default:
		return true
	}
}

func filterRepeatedToolCalls(req StandardRequest, calls []ParsedToolCall) ([]ParsedToolCall, []ParsedToolCall) {
	if len(calls) == 0 || req.RepeatedToolCount < 3 || strings.TrimSpace(req.RepeatedToolSignature) == "" {
		return calls, nil
	}
	kept := make([]ParsedToolCall, 0, len(calls))
	blocked := []ParsedToolCall{}
	for _, call := range calls {
		if parsedToolCallSignature(call) == req.RepeatedToolSignature {
			blocked = append(blocked, call)
			continue
		}
		kept = append(kept, call)
	}
	return kept, blocked
}

func shouldBufferStreamTextDeltas(req StandardRequest) bool {
	return req.ToolEnabled && len(req.Tools) > 0
}

func parsedToolCallSignature(call ParsedToolCall) string {
	name := strings.ToLower(strings.TrimSpace(call.Name))
	args := "{}"
	switch v := call.Input.(type) {
	case nil:
		args = "{}"
	case string:
		args = v
	default:
		args = mustJSON(v)
	}
	return name + "\x00" + normalizeToolSignatureArgs(args)
}

func formatToolSignatureForPrompt(signature string) string {
	signature = strings.TrimSpace(signature)
	if signature == "" {
		return ""
	}
	parts := strings.SplitN(signature, "\x00", 2)
	if len(parts) != 2 {
		return truncate(signature, 400)
	}
	tool := strings.TrimSpace(parts[0])
	args := strings.TrimSpace(parts[1])
	if args == "" {
		args = "{}"
	}
	return "tool=" + firstNonEmpty(tool, "unknown") + " args=" + truncate(args, 360)
}

func normalizeToolSignatureArgs(args string) string {
	args = strings.TrimSpace(args)
	if args == "" {
		return "{}"
	}
	var decoded any
	if json.Unmarshal([]byte(args), &decoded) == nil {
		normalized, _ := json.Marshal(decoded)
		return string(normalized)
	}
	return strings.Join(strings.Fields(args), " ")
}

func isRepeatedToolCallBlockedResult(result CompletionResult) bool {
	return strings.HasPrefix(result.FinishReason, "repeated_tool_call:")
}

func isInvalidToolArgsResult(result CompletionResult) bool {
	return strings.HasPrefix(result.FinishReason, "invalid_tool_args:")
}

func invalidToolArgsName(result CompletionResult) string {
	if strings.HasPrefix(result.FinishReason, "invalid_tool_args:") {
		if name := strings.TrimSpace(strings.TrimPrefix(result.FinishReason, "invalid_tool_args:")); name != "" {
			return name
		}
	}
	return "unknown"
}

func invalidToolArgsFingerprint(req StandardRequest, result CompletionResult) string {
	if !isInvalidToolArgsResult(result) {
		return ""
	}
	for _, signature := range result.InvalidToolCallSignatures {
		if strings.TrimSpace(signature) != "" {
			return strings.TrimSpace(signature)
		}
	}
	parsed := parseToolCalls(toolParseText(result), req.Tools)
	if len(parsed) > 0 {
		_, invalid := filterInvalidToolCalls(parsed)
		if len(invalid) > 0 {
			return parsedToolCallSignature(invalid[0])
		}
	}
	tool := strings.ToLower(strings.TrimSpace(invalidToolArgsName(result)))
	if tool == "" || tool == "unknown" {
		tool = strings.ToLower(strings.TrimSpace(services.ExtractAttemptedToolName(toolParseText(result), req.ToolNames)))
	}
	text := normalizeToolSignatureArgs(toolParseText(result))
	if text != "" && text != "{}" {
		return tool + "\x00" + text
	}
	return tool
}

func repeatedToolCallName(req StandardRequest, result CompletionResult) string {
	if strings.HasPrefix(result.FinishReason, "repeated_tool_call:") {
		if name := strings.TrimSpace(strings.TrimPrefix(result.FinishReason, "repeated_tool_call:")); name != "" {
			return name
		}
	}
	return strings.TrimSpace(req.RepeatedToolName)
}

func firstRepeatedToolName(calls []ParsedToolCall) string {
	for _, call := range calls {
		if strings.TrimSpace(call.Name) != "" {
			return strings.TrimSpace(call.Name)
		}
	}
	return ""
}

func asyncDeleteChat(client *QwenClient, token, cookies, chatID string) {
	if client == nil || strings.TrimSpace(token) == "" || strings.TrimSpace(chatID) == "" {
		return
	}
	credsCopy := qwenCreds(token, cookies)
	chatIDCopy := chatID
	go client.DeleteChat(context.Background(), credsCopy, chatIDCopy)
}

func (app *App) recoverBlockedToolNameOutput(ctx context.Context, acc *Account, req StandardRequest, result CompletionResult) CompletionResult {
	if acc == nil || !req.ToolEnabled || len(req.Tools) == 0 || len(result.ToolCalls) > 0 || !isBlockedToolNameOutput(result, req.ToolNames) {
		return result
	}
	working := result
	bestText := CompletionResult{}
	for attempt := 1; attempt <= 2; attempt++ {
		attempted := firstBlockedToolName(working, req.ToolNames)
		retryPrompt := injectBlockedToolNameRetryGuard(req.Prompt, working.AnswerText, attempted)
		app.logInfo(ctx, "[Retry] blocked tool-name contamination", "attempt", attempt, "blocked_tool", firstNonEmpty(attempted, "-"))
		retryResult, err := app.runToolMarkupRecoveryAttempt(ctx, acc, req, retryPrompt, "blocked_tool_name_retry")
		if err != nil {
			app.logWarn(ctx, "[Retry] blocked tool-name retry failed", "attempt", attempt, "blocked_tool", firstNonEmpty(attempted, "-"), "error", err)
			continue
		}
		if len(retryResult.ToolCalls) > 0 {
			app.logInfo(ctx, "[Retry] blocked tool-name retry produced tool calls", "attempt", attempt, "tools", parsedToolNames(retryResult.ToolCalls))
			return retryResult
		}
		if strings.TrimSpace(retryResult.AnswerText) != "" && !isBlockedToolNameOutput(retryResult, req.ToolNames) && !hasTextualToolMarker(retryResult.AnswerText) {
			bestText = retryResult
			break
		}
		working = retryResult
	}
	if strings.TrimSpace(bestText.AnswerText) != "" || strings.TrimSpace(bestText.ReasoningText) != "" {
		return bestText
	}
	sanitized := result
	suppressBlockedToolNameOutput(&sanitized, req.ToolNames)
	return sanitized
}

func (app *App) recoverUnparsedToolMarkup(ctx context.Context, acc *Account, req StandardRequest, result CompletionResult) CompletionResult {
	if acc == nil || !req.ToolEnabled || len(req.Tools) == 0 || len(result.ToolCalls) > 0 || !hasTextualToolMarker(toolParseText(result)) {
		return result
	}
	working := result
	for attempt := 1; attempt <= 2 && services.IsToolCallTruncated(toolParseText(working)); attempt++ {
		assistantCtx, followup := services.BuildContinuationPrompt(toolParseText(working), 2000)
		contPrompt := strings.TrimRight(req.Prompt, " \t\r\n") + "\n\nAssistant: " + assistantCtx + "\n\nHuman: " + followup + "\n\nAssistant:"
		app.logInfo(ctx, "[TruncRecover] detected unclosed tool call", "attempt", attempt, "len", len(toolParseText(working)))
		cont, err := app.runToolMarkupRecoveryAttempt(ctx, acc, req, contPrompt, "truncation_continuation")
		if err != nil {
			app.logWarn(ctx, "[TruncRecover] continuation failed", "attempt", attempt, "error", err)
			break
		}
		if len(cont.ToolCalls) > 0 {
			app.logInfo(ctx, "[TruncRecover] continuation produced tool calls", "attempt", attempt, "tools", parsedToolNames(cont.ToolCalls))
			return cont
		}
		if strings.TrimSpace(cont.AnswerText) == "" {
			app.logInfo(ctx, "[TruncRecover] empty continuation, stopping", "attempt", attempt)
			break
		}
		deduped := services.DeduplicateContinuation(working.AnswerText, cont.AnswerText)
		if strings.TrimSpace(deduped) == "" {
			app.logInfo(ctx, "[TruncRecover] continuation fully overlapped existing output", "attempt", attempt)
			break
		}
		working.AnswerText += deduped
		working.ReasoningText += cont.ReasoningText
		working.Events = append(working.Events, cont.Events...)
		working.ToolCalls = parseToolCalls(toolParseText(working), req.Tools)
		if len(working.ToolCalls) > 0 {
			working.FinishReason = "tool_calls"
			app.logInfo(ctx, "[TruncRecover] merged continuation parsed tool calls", "attempt", attempt, "tools", parsedToolNames(working.ToolCalls), "len", len(toolParseText(working)))
			return working
		}
		if !services.IsToolCallTruncated(toolParseText(working)) {
			break
		}
	}

	bestText := CompletionResult{}
	for attempt := 1; attempt <= 2; attempt++ {
		attempted := services.ExtractAttemptedToolName(toolParseText(working), req.ToolNames)
		retryPrompt := injectMalformedToolRetryGuard(req.Prompt, toolParseText(working), attempted)
		app.logInfo(ctx, "[Retry] malformed textual tool contract", "attempt", attempt, "attempted_tool", firstNonEmpty(attempted, "-"))
		retryResult, err := app.runToolMarkupRecoveryAttempt(ctx, acc, req, retryPrompt, "malformed_tool_retry")
		if err != nil {
			app.logWarn(ctx, "[Retry] malformed textual tool retry failed", "attempt", attempt, "error", err)
			continue
		}
		if len(retryResult.ToolCalls) > 0 {
			app.logInfo(ctx, "[Retry] malformed textual tool retry produced tool calls", "attempt", attempt, "tools", parsedToolNames(retryResult.ToolCalls))
			return retryResult
		}
		if isInvalidToolArgsResult(retryResult) {
			app.logWarn(ctx, "[Retry] malformed textual tool retry produced invalid tool arguments", "attempt", attempt, "tool", invalidToolArgsName(retryResult))
			fixed := app.recoverInvalidToolCallArgs(ctx, acc, req, retryResult)
			if len(fixed.ToolCalls) > 0 {
				return fixed
			}
			retryResult = fixed
		}
		if strings.TrimSpace(toolParseText(retryResult)) != "" && !hasTextualToolMarker(toolParseText(retryResult)) {
			bestText = retryResult
			break
		}
		working = retryResult
	}
	if strings.TrimSpace(bestText.AnswerText) != "" || strings.TrimSpace(bestText.ReasoningText) != "" {
		return bestText
	}
	return result
}

func (app *App) recoverMissingToolContinuation(ctx context.Context, acc *Account, req StandardRequest, result CompletionResult) CompletionResult {
	if acc == nil || !shouldForceToolContinuation(req, result) {
		return result
	}
	working := result
	bestText := CompletionResult{}
	for attempt := 1; attempt <= 2; attempt++ {
		retryPrompt := injectMissingToolContinuationGuard(req.Prompt, toolParseText(working))
		app.logInfo(ctx, "[Retry] missing tool continuation", "attempt", attempt, "answer_len", len(working.AnswerText), "reasoning_len", len(working.ReasoningText))
		retryResult, err := app.runToolMarkupRecoveryAttempt(ctx, acc, req, retryPrompt, "missing_tool_continuation_retry")
		if err != nil {
			app.logWarn(ctx, "[Retry] missing tool continuation retry failed", "attempt", attempt, "error", err)
			continue
		}
		if len(retryResult.ToolCalls) > 0 {
			app.logInfo(ctx, "[Retry] missing tool continuation produced tool calls", "attempt", attempt, "tools", parsedToolNames(retryResult.ToolCalls))
			return retryResult
		}
		if isInvalidToolArgsResult(retryResult) {
			app.logWarn(ctx, "[Retry] missing tool continuation produced invalid tool arguments", "attempt", attempt, "tool", invalidToolArgsName(retryResult))
			fixed := app.recoverInvalidToolCallArgs(ctx, acc, req, retryResult)
			if len(fixed.ToolCalls) > 0 {
				return fixed
			}
			retryResult = fixed
		}
		if isAcceptableNoToolContinuationText(req, retryResult) {
			bestText = retryResult
			break
		}
		working = retryResult
	}
	if strings.TrimSpace(bestText.AnswerText) != "" || strings.TrimSpace(bestText.ReasoningText) != "" {
		return bestText
	}
	return result
}

func (app *App) recoverMissingInitialToolCall(ctx context.Context, acc *Account, req StandardRequest, result CompletionResult) CompletionResult {
	if acc == nil || !shouldRecoverMissingInitialToolCall(req, result) {
		return result
	}
	working := result
	bestText := CompletionResult{}
	for attempt := 1; attempt <= 2; attempt++ {
		retryPrompt := injectMissingInitialToolCallGuard(req.Prompt, toolParseText(working))
		app.logInfo(ctx, "[Retry] missing initial tool call", "attempt", attempt, "answer_len", len(working.AnswerText), "reasoning_len", len(working.ReasoningText))
		retryResult, err := app.runToolMarkupRecoveryAttempt(ctx, acc, req, retryPrompt, "missing_initial_tool_call_retry")
		if err != nil {
			app.logWarn(ctx, "[Retry] missing initial tool call retry failed", "attempt", attempt, "error", err)
			continue
		}
		if len(retryResult.ToolCalls) > 0 {
			app.logInfo(ctx, "[Retry] missing initial tool call produced tool calls", "attempt", attempt, "tools", parsedToolNames(retryResult.ToolCalls))
			return retryResult
		}
		if isInvalidToolArgsResult(retryResult) {
			app.logWarn(ctx, "[Retry] missing initial tool call produced invalid tool arguments", "attempt", attempt, "tool", invalidToolArgsName(retryResult))
			fixed := app.recoverInvalidToolCallArgs(ctx, acc, req, retryResult)
			if len(fixed.ToolCalls) > 0 {
				return fixed
			}
			retryResult = fixed
		}
		if !shouldRecoverMissingInitialToolCall(req, retryResult) {
			bestText = retryResult
			break
		}
		working = retryResult
	}
	if strings.TrimSpace(bestText.AnswerText) != "" || strings.TrimSpace(bestText.ReasoningText) != "" {
		return bestText
	}
	return result
}

func (app *App) recoverInvalidToolCallArgs(ctx context.Context, acc *Account, req StandardRequest, result CompletionResult) CompletionResult {
	if acc == nil || !req.ToolEnabled || len(req.Tools) == 0 || !isInvalidToolArgsResult(result) {
		return result
	}
	working := result
	bestText := CompletionResult{}
	seenInvalid := map[string]bool{}
	if fingerprint := invalidToolArgsFingerprint(req, working); fingerprint != "" {
		seenInvalid[fingerprint] = true
	}
	for attempt := 1; attempt <= 2; attempt++ {
		retryPrompt := injectInvalidToolArgsRetryGuard(req.Prompt, invalidToolArgsName(working), toolParseText(working), invalidToolArgsRecoveryGuidance(req, working))
		app.logInfo(ctx, "[Retry] invalid tool arguments", "attempt", attempt, "tool", invalidToolArgsName(working))
		retryResult, err := app.runToolMarkupRecoveryAttempt(ctx, acc, req, retryPrompt, "invalid_tool_args_retry")
		if err != nil {
			app.logWarn(ctx, "[Retry] invalid tool arguments retry failed", "attempt", attempt, "tool", invalidToolArgsName(working), "error", err)
			continue
		}
		if len(retryResult.ToolCalls) > 0 {
			app.logInfo(ctx, "[Retry] invalid tool arguments retry produced tool calls", "attempt", attempt, "tools", parsedToolNames(retryResult.ToolCalls))
			return retryResult
		}
		if strings.TrimSpace(toolParseText(retryResult)) != "" && !isInvalidToolArgsResult(retryResult) && !isBlockedToolNameOutput(retryResult, req.ToolNames) && !hasTextualToolMarker(toolParseText(retryResult)) {
			bestText = retryResult
			break
		}
		if isInvalidToolArgsResult(retryResult) {
			fingerprint := invalidToolArgsFingerprint(req, retryResult)
			if fingerprint != "" && seenInvalid[fingerprint] {
				app.logWarn(ctx, "[Retry] invalid tool arguments repeated same invalid call; stopping recovery",
					"attempt", attempt,
					"tool", invalidToolArgsName(retryResult),
					"fingerprint", truncate(fingerprint, 160),
				)
				working = retryResult
				break
			}
			if fingerprint != "" {
				seenInvalid[fingerprint] = true
			}
		}
		working = retryResult
		if !isInvalidToolArgsResult(working) {
			working.FinishReason = result.FinishReason
		}
	}
	if strings.TrimSpace(bestText.AnswerText) != "" || strings.TrimSpace(bestText.ReasoningText) != "" {
		return bestText
	}
	return result
}

func (app *App) recoverRepeatedToolCall(ctx context.Context, acc *Account, req StandardRequest, result CompletionResult) CompletionResult {
	if acc == nil || !req.ToolEnabled || len(req.Tools) == 0 || !isRepeatedToolCallBlockedResult(result) {
		return result
	}
	working := result
	bestText := CompletionResult{}
	for attempt := 1; attempt <= 2; attempt++ {
		retryPrompt := injectRepeatedToolCallRetryGuard(req.Prompt, repeatedToolCallName(req, working), req.RepeatedToolCount, formatToolSignatureForPrompt(req.RepeatedToolSignature))
		app.logInfo(ctx, "[Retry] repeated tool call loop", "attempt", attempt, "tool", repeatedToolCallName(req, working), "history_count", req.RepeatedToolCount)
		retryResult, err := app.runToolMarkupRecoveryAttempt(ctx, acc, req, retryPrompt, "repeated_tool_call_retry")
		if err != nil {
			app.logWarn(ctx, "[Retry] repeated tool call retry failed", "attempt", attempt, "tool", repeatedToolCallName(req, working), "error", err)
			continue
		}
		if len(retryResult.ToolCalls) > 0 {
			app.logInfo(ctx, "[Retry] repeated tool call retry produced distinct tool calls", "attempt", attempt, "tools", parsedToolNames(retryResult.ToolCalls))
			return retryResult
		}
		if isAcceptableNoToolContinuationText(req, retryResult) {
			bestText = retryResult
			break
		}
		working = retryResult
		if !isRepeatedToolCallBlockedResult(working) {
			working.FinishReason = result.FinishReason
		}
	}
	if strings.TrimSpace(bestText.AnswerText) != "" || strings.TrimSpace(bestText.ReasoningText) != "" {
		return bestText
	}
	return result
}

func isAcceptableNoToolContinuationText(req StandardRequest, result CompletionResult) bool {
	text := strings.TrimSpace(toolParseText(result))
	if text == "" {
		return false
	}
	if !isLikelyCompletedToolTurn(text) {
		return false
	}
	return !shouldRejectUngroundedFinalClaim(req, result)
}

func (app *App) runToolMarkupRecoveryAttempt(ctx context.Context, acc *Account, req StandardRequest, prompt, reason string) (CompletionResult, error) {
	chatID, err := app.client.CreateChat(ctx, qwenCreds(acc.Token, acc.Cookies), req.ResolvedModel, req.ChatType)
	if err != nil {
		app.classifyAccountError(acc, err)
		return CompletionResult{}, err
	}
	defer asyncDeleteChat(app.client, acc.Token, acc.Cookies, chatID)
	app.logInfo(ctx, "[Retry] recovery chat created", "reason", reason, "recovery_chat_id", chatID)
	payload := buildChatPayload(chatID, req.ResolvedModel, prompt, req.ToolEnabled, req.UpstreamFiles, req.ChatType, nil, req.ThinkingEnabled, req.EnableSearch)
	result := CompletionResult{FinishReason: "stop"}
	start := time.Now()
	var sieve *toolcall.ToolSieve
	if req.ToolEnabled {
		sieve = toolcall.NewToolSieve(req.Tools)
	}
	err = app.client.StreamChat(ctx, qwenCreds(acc.Token, acc.Cookies), chatID, payload, func(evt UpstreamEvent) error {
		result.Events = append(result.Events, evt)
		if evt.Type != "delta" || evt.Content == "" {
			return nil
		}
		if strings.Contains(evt.Phase, "thinking") || strings.Contains(evt.Phase, "reasoning") {
			result.ReasoningText += evt.Content
			if sieve != nil {
				for _, sieveEvt := range sieve.ProcessChunk(evt.Content) {
					if sieveEvt.Type == "tool_calls" && len(sieveEvt.Calls) > 0 {
						captured, err := app.captureSieveToolCalls(ctx, req, &result, sieveEvt.Calls, "recovery:"+reason+":tool_sieve_reasoning_stream", nil)
						if err != nil {
							return err
						}
						if captured {
							return errToolSieveDetected
						}
						continue
					}
				}
				if blocked := extractBlockedToolNames(result.ReasoningText, req.ToolNames); len(blocked) > 0 {
					result.FinishReason = "blocked_tool_name:" + blocked[0]
					app.logWarn(ctx, "[Retry] recovery blocked contaminated reasoning", "reason", reason, "preview", promptTail(result.ReasoningText, 120), "blocked_tool", blocked[0])
					suppressBlockedToolNameOutput(&result, req.ToolNames)
					return errToolSieveDetected
				}
			}
			return nil
		}
		result.AnswerText += evt.Content
		if sieve != nil {
			for _, sieveEvt := range sieve.ProcessChunk(evt.Content) {
				if sieveEvt.Type == "tool_calls" && len(sieveEvt.Calls) > 0 {
					captured, err := app.captureSieveToolCalls(ctx, req, &result, sieveEvt.Calls, "recovery:"+reason+":tool_sieve_stream", nil)
					if err != nil {
						return err
					}
					if captured {
						return errToolSieveDetected
					}
					continue
				}
			}
			if blocked := extractBlockedToolNames(result.AnswerText, req.ToolNames); len(blocked) > 0 {
				result.FinishReason = "blocked_tool_name:" + blocked[0]
				app.logWarn(ctx, "[Retry] recovery blocked contaminated output", "reason", reason, "preview", promptTail(result.AnswerText, 120), "blocked_tool", blocked[0])
				suppressBlockedToolNameOutput(&result, req.ToolNames)
				return errToolSieveDetected
			}
		}
		return nil
	})
	if err != nil && !errors.Is(err, errToolSieveDetected) {
		app.classifyAccountError(acc, err)
		app.logWarn(ctx, "[Retry] recovery upstream stream failed", "reason", reason, "recovery_chat_id", chatID, "error", err, "events", len(result.Events), "duration_ms", time.Since(start).Milliseconds())
		return result, err
	}
	app.logInfo(ctx, "[Retry] recovery stream completed", "reason", reason, "recovery_chat_id", chatID, "events", len(result.Events), "answer_len", len(result.AnswerText), "reasoning_len", len(result.ReasoningText), "duration_ms", time.Since(start).Milliseconds())
	if result.AnswerText != "" || result.ReasoningText != "" {
		app.logInfo(ctx, "[Retry] recovery response summary", "reason", reason, "answer_tail", promptTail(result.AnswerText, 600), "reasoning_tail", promptTail(result.ReasoningText, 300))
	}
	if req.ToolEnabled && len(result.ToolCalls) == 0 && sieve != nil {
		for _, sieveEvt := range sieve.Flush() {
			if sieveEvt.Type == "tool_calls" && len(sieveEvt.Calls) > 0 {
				captured, _ := app.captureSieveToolCalls(ctx, req, &result, sieveEvt.Calls, "recovery:"+reason+":tool_sieve_flush", nil)
				if captured {
					break
				}
			}
		}
	}
	if req.ToolEnabled && len(result.ToolCalls) == 0 {
		parsed := parseToolCalls(toolParseText(result), req.Tools)
		if len(parsed) > 0 {
			app.captureDetectedToolCalls(ctx, req, &result, parsed, "recovery:"+reason+":final_text_parse")
		}
	}
	if req.ToolEnabled && len(result.ToolCalls) == 0 {
		result = markInvalidToolArgsFromTextualMarker(req, result)
	}
	if result.AnswerText == "" && result.ReasoningText == "" && len(result.ToolCalls) == 0 {
		result.FinishReason = "empty"
	}
	return result, nil
}

func markInvalidToolArgsFromTextualMarker(req StandardRequest, result CompletionResult) CompletionResult {
	if len(result.ToolCalls) > 0 || !hasTextualToolMarker(toolParseText(result)) {
		return result
	}
	attempted := services.ExtractAttemptedToolName(toolParseText(result), req.ToolNames)
	if attempted == "" {
		attempted = "unknown"
	}
	result.FinishReason = "invalid_tool_args:" + attempted
	return result
}

func injectMalformedToolRetryGuard(prompt, malformedText, attemptedTool string) string {
	tool := strings.TrimSpace(attemptedTool)
	if tool == "" {
		tool = "the needed tool"
	}
	tail := promptTail(malformedText, 500)
	guard := "[MANDATORY]: The previous assistant output contained malformed textual QNML/XML/JSON tool-call markup and was blocked before client delivery.\n" +
		"Do NOT continue, quote, or repair that partial markup. Start over with one fresh, complete tool call only, using exact available tool and parameter names.\n" +
		"For QNML use exactly this shell: <|QNML|tool_calls> then <|QNML|invoke name=\"TOOL_NAME\"> then <|QNML|parameter name=\"ARG\"><![CDATA[value]]></|QNML|parameter> then close invoke and tool_calls.\n" +
		toolArgumentRetryInstruction() +
		"Intended tool if recoverable: " + tool + "\n" +
		"Malformed tail for context only, do not copy it:\n```\n" + tail + "\n```"
	trimmed := strings.TrimRight(prompt, " \t\r\n")
	if strings.HasSuffix(trimmed, "Assistant:") {
		return strings.TrimRight(trimmed[:len(trimmed)-len("Assistant:")], " \t\r\n") + "\n\n" + guard + "\n\nAssistant:"
	}
	return trimmed + "\n\n" + guard + "\n\nAssistant:"
}

func injectBlockedToolNameRetryGuard(prompt, blockedText, attemptedTool string) string {
	tool := strings.TrimSpace(attemptedTool)
	if tool == "" {
		tool = "the needed tool"
	}
	tail := promptTail(blockedText, 500)
	guard := "[MANDATORY]: The previous assistant output incorrectly claimed a listed client-side QNML tool was unavailable and was blocked before client delivery.\n" +
		"That claim is invalid. Tools listed in the QNML protocol are client-side actions, not Qwen native tools.\n" +
		"Do NOT apologize, explain tool availability, or output prose. Start over with one fresh, complete QNML tool call only, using exact available tool and parameter names.\n" +
		"For QNML use exactly this shell: <|QNML|tool_calls> then <|QNML|invoke name=\"TOOL_NAME\"> then <|QNML|parameter name=\"ARG\"><![CDATA[value]]></|QNML|parameter> then close invoke and tool_calls.\n" +
		toolArgumentRetryInstruction() +
		"Intended tool if recoverable: " + tool + "\n" +
		"Blocked text for context only, do not copy it:\n```\n" + tail + "\n```"
	trimmed := strings.TrimRight(prompt, " \t\r\n")
	if strings.HasSuffix(trimmed, "Assistant:") {
		return strings.TrimRight(trimmed[:len(trimmed)-len("Assistant:")], " \t\r\n") + "\n\n" + guard + "\n\nAssistant:"
	}
	return trimmed + "\n\n" + guard + "\n\nAssistant:"
}

func injectMissingToolContinuationGuard(prompt, answerText string) string {
	tail := promptTail(answerText, 500)
	if strings.TrimSpace(tail) == "" {
		tail = "(empty)"
	}
	guard := "[MANDATORY]: The previous assistant turn came immediately after a client tool result but it only produced narration/plan text and did not execute the next action.\n" +
		"Do NOT repeat the round label, status summary, or plan. Continue from the latest tool result.\n" +
		"If the task is not fully complete, your next assistant turn MUST be exactly one fresh, complete QNML tool call and nothing else.\n" +
		"If the original task requires final checks or verification, do not claim completion unless the latest tool evidence proves those checks passed; otherwise issue a verification tool call or state the unverifiable result honestly.\n" +
		"For QNML use exactly this shell: <|QNML|tool_calls> then <|QNML|invoke name=\"TOOL_NAME\"> then <|QNML|parameter name=\"ARG\"><![CDATA[value]]></|QNML|parameter> then close invoke and tool_calls.\n" +
		toolArgumentRetryInstruction() +
		"Only if the task is truly complete may you return the final completion answer instead of a tool call.\n" +
		"Previous narration to avoid repeating:\n```\n" + tail + "\n```"
	trimmed := strings.TrimRight(prompt, " \t\r\n")
	if strings.HasSuffix(trimmed, "Assistant:") {
		return strings.TrimRight(trimmed[:len(trimmed)-len("Assistant:")], " \t\r\n") + "\n\n" + guard + "\n\nAssistant:"
	}
	return trimmed + "\n\n" + guard + "\n\nAssistant:"
}

func injectMissingInitialToolCallGuard(prompt, answerText string) string {
	tail := promptTail(answerText, 500)
	if strings.TrimSpace(tail) == "" {
		tail = "(empty)"
	}
	guard := "[MANDATORY]: The previous assistant turn answered an executable user task with narration, a plan, or an unverified completion claim but did not emit any client-side tool call.\n" +
		"Do NOT repeat the plan or say you will start. If the user's task requires file access, shell execution, search, browser work, skills, agents, or artifact verification, your next assistant turn MUST be exactly one fresh, complete QNML tool call and nothing else.\n" +
		"If no client-side action is actually required, answer normally and explain that no tool execution is needed. If the task cannot be executed, state that limitation honestly instead of claiming completion.\n" +
		"For QNML use exactly this shell: <|QNML|tool_calls> then <|QNML|invoke name=\"TOOL_NAME\"> then <|QNML|parameter name=\"ARG\"><![CDATA[value]]></|QNML|parameter> then close invoke and tool_calls.\n" +
		toolArgumentRetryInstruction() +
		"Previous narration to avoid repeating:\n```\n" + tail + "\n```"
	trimmed := strings.TrimRight(prompt, " \t\r\n")
	if strings.HasSuffix(trimmed, "Assistant:") {
		return strings.TrimRight(trimmed[:len(trimmed)-len("Assistant:")], " \t\r\n") + "\n\n" + guard + "\n\nAssistant:"
	}
	return trimmed + "\n\n" + guard + "\n\nAssistant:"
}

func injectInvalidToolArgsRetryGuard(prompt, toolName, attemptedText, extraGuidance string) string {
	tool := strings.TrimSpace(toolName)
	if tool == "" {
		tool = "the attempted tool"
	}
	tail := promptTail(attemptedText, 500)
	if strings.TrimSpace(tail) == "" {
		tail = "(empty)"
	}
	guidance := strings.TrimSpace(extraGuidance)
	if guidance != "" {
		guidance += "\n"
	}
	guard := "[MANDATORY]: The previous assistant turn attempted a client-side tool call with incomplete or invalid arguments, and it was blocked before client delivery.\n" +
		"Do NOT repeat the same incomplete arguments. Continue from the latest confirmed state and emit exactly one fresh, complete QNML tool call with all required target and action/content fields.\n" +
		"For file mutation tools such as patch/Edit/MultiEdit/Write/write_file, include both the target file/path and the actual change payload: content field, diff, old/new strings, edits, hunks, or replacement text. A mode-only call is invalid; an empty content value is valid only when the content field is explicitly present for empty-file creation/truncation.\n" +
		"Listed QNML action names are executable client-side tools; do not check native availability or claim they do not exist.\n" +
		"For shell tools such as Bash, PowerShell, terminal, exec, shell, process, or execute_code, include a complete non-empty command/script/code argument.\n" +
		"For read/search tools such as Read, read_file, read, Grep, grep, find, or ls, include a concrete non-empty path/query/pattern argument.\n" +
		"For ordered or numbered workflows, do not skip ahead. If a future step/round was blocked because an earlier planned step is missing, go back to the earliest missing step and execute that step only.\n" +
		guidance +
		"For QNML use exactly this shell: <|QNML|tool_calls> then <|QNML|invoke name=\"TOOL_NAME\"> then <|QNML|parameter name=\"ARG\"><![CDATA[value]]></|QNML|parameter> then close invoke and tool_calls.\n" +
		"Blocked tool: " + tool + "\n" +
		"Previous invalid output for context only, do not copy it:\n```\n" + tail + "\n```"
	trimmed := strings.TrimRight(prompt, " \t\r\n")
	if strings.HasSuffix(trimmed, "Assistant:") {
		return strings.TrimRight(trimmed[:len(trimmed)-len("Assistant:")], " \t\r\n") + "\n\n" + guard + "\n\nAssistant:"
	}
	return trimmed + "\n\n" + guard + "\n\nAssistant:"
}

func injectRepeatedToolCallRetryGuard(prompt, toolName string, count int, blockedAction string) string {
	tool := strings.TrimSpace(toolName)
	if tool == "" {
		tool = "the repeated tool"
	}
	if count < 2 {
		count = 2
	}
	action := strings.TrimSpace(blockedAction)
	if action == "" {
		action = tool
	}
	guard := "[MANDATORY]: The previous assistant turn attempted the exact same client-side tool call that already appeared repeatedly in recent history.\n" +
		"That repeated call was blocked before client delivery to prevent a tool loop.\n" +
		"Do NOT call the same action with the same arguments again. Treat that action as already exhausted for this turn.\n" +
		"Use the latest tool result and choose a different next action, change arguments based on new evidence, read/search/verify current state, record the limitation/unavailable state honestly, or finish only if the existing tool evidence already supports completion.\n" +
		"If the repeated action was a bookkeeping write such as appending a log or note and the latest tool history already shows it succeeded, skip that bookkeeping action and advance to the next distinct required step.\n" +
		"For QNML use exactly this shell: <|QNML|tool_calls> then <|QNML|invoke name=\"TOOL_NAME\"> then <|QNML|parameter name=\"ARG\"><![CDATA[value]]></|QNML|parameter> then close invoke and tool_calls.\n" +
		toolArgumentRetryInstruction() +
		fmt.Sprintf("Blocked repeated action: %s, repeated_count: %d.\n", action, count)
	trimmed := strings.TrimRight(prompt, " \t\r\n")
	if strings.HasSuffix(trimmed, "Assistant:") {
		return strings.TrimRight(trimmed[:len(trimmed)-len("Assistant:")], " \t\r\n") + "\n\n" + guard + "\n\nAssistant:"
	}
	return trimmed + "\n\n" + guard + "\n\nAssistant:"
}

func toolArgumentRetryInstruction() string {
	return "Listed QNML action names are executable client-side tools; do not check native availability or claim they do not exist. " +
		"If calling a shell tool such as Bash, PowerShell, terminal, exec, shell, or execute_code, include a complete non-empty command/script/code argument. " +
		"If calling a write-file tool such as Write, write_file, or write, include a non-empty path/file_path and a content argument; content may be empty only for intentional empty-file creation/truncation. " +
		"If calling an edit/patch tool such as Edit, MultiEdit, edit, apply_patch, or patch, include the target path and actual change payload. " +
		"If calling a read-file tool such as Read, read_file, or read, include one non-empty path/file_path.\n"
}

func invalidToolMarkupFallback(req StandardRequest, result CompletionResult) string {
	attempted := ""
	if isInvalidToolArgsResult(result) {
		attempted = invalidToolArgsName(result)
	}
	if attempted == "" || attempted == "unknown" {
		attempted = services.ExtractAttemptedToolName(toolParseText(result), req.ToolNames)
	}
	if attempted == "" {
		attempted = "the requested tool"
	}
	return "Invalid tool-call markup for " + attempted + " was blocked and could not be recovered. Continue by issuing one fresh complete QNML tool call with all required arguments."
}

func missingInitialToolCallFallback() string {
	return "The task appears to require a client-side tool action, but the upstream model returned narration instead of a tool call and recovery did not produce a valid call. No execution or verification has been performed."
}

func invalidToolArgsFallback(result CompletionResult) string {
	return "A client-side tool call for " + invalidToolArgsName(result) + " was blocked because its arguments were incomplete or invalid, and recovery did not produce a valid replacement. No client tool execution was performed for that attempted call."
}

func (app *App) classifyAccountError(acc *Account, err error) {
	app.classifyAccountErrorFor(acc, err, accountUsageChat)
}

func (app *App) classifyAccountErrorFor(acc *Account, err error, usage string) {
	if acc == nil || err == nil {
		return
	}
	msg := err.Error()
	lower := strings.ToLower(msg)
	if isRateLimitErrorMessage(lower) {
		usage = normalizeAccountUsage(usage)
		app.accounts.MarkRateLimitedFor(acc, usage, app.accountErrorCooldown(lower), msg)
		if app.logger != nil {
			app.logger.Warn("账号进入分用途限额冷却", "account", acc.Email, "usage", usage, "reason", rateLimitReasonForUsage(usage), "error", truncate(msg, 240))
		}
		return
	}
	if isModelNotFoundErrorMessage(lower) {
		if app.logger != nil {
			app.logger.Warn("上游模型不可用，未标记账号失效", "account", acc.Email, "error", truncate(msg, 240))
		}
		return
	}
	if isAuthErrorMessage(lower) {
		app.accounts.MarkInvalid(acc, "auth_error", msg)
		if app.logger != nil {
			app.logger.Warn("账号认证失败，已标记不可用", "account", acc.Email, "error", truncate(msg, 240))
		}
	}
}

func (app *App) accountErrorCooldown(lower string) int {
	return rateLimitCooldownSeconds(app.settings, lower)
}

func rateLimitCooldownSeconds(settings Settings, lower string) int {
	lower = strings.ToLower(lower)
	if strings.Contains(lower, "today's usage") ||
		strings.Contains(lower, "todays usage") ||
		strings.Contains(lower, "daily usage") ||
		strings.Contains(lower, "daily limit") ||
		strings.Contains(lower, "upper limit for today") {
		now := time.Now()
		nextDay := time.Date(now.Year(), now.Month(), now.Day()+1, 0, 10, 0, 0, now.Location())
		return maxInt(int(time.Until(nextDay).Seconds()), settings.RateLimitBaseCooldown)
	}
	return settings.RateLimitBaseCooldown
}

func isRateLimitErrorMessage(lower string) bool {
	lower = strings.ToLower(lower)
	if strings.Contains(lower, "http 429") ||
		strings.Contains(lower, "status 429") ||
		strings.Contains(lower, "status=429") ||
		strings.Contains(lower, "code=429") ||
		strings.Contains(lower, "code 429") {
		return true
	}
	for _, marker := range []string{
		"ratelimited",
		"rate_limited",
		"rate limited",
		"rate limit",
		"too many requests",
		"upper limit",
		"usage limit",
		"today's usage",
		"todays usage",
		"daily usage",
		"daily limit",
		"quota",
		"free quota",
		"insufficient quota",
	} {
		if strings.Contains(lower, marker) {
			return true
		}
	}
	return false
}

func isModelNotFoundErrorMessage(lower string) bool {
	lower = strings.ToLower(lower)
	return strings.Contains(lower, "model not found") ||
		strings.Contains(lower, "model not exist") ||
		(strings.Contains(lower, "not_found") && strings.Contains(lower, "model")) ||
		(strings.Contains(lower, "not found") && strings.Contains(lower, "model"))
}

func isAuthErrorMessage(lower string) bool {
	lower = strings.ToLower(lower)
	if strings.Contains(lower, "http 401") ||
		strings.Contains(lower, "status 401") ||
		strings.Contains(lower, "status=401") ||
		strings.Contains(lower, "code=401") ||
		strings.Contains(lower, "code 401") ||
		strings.Contains(lower, "http 403") ||
		strings.Contains(lower, "status 403") ||
		strings.Contains(lower, "status=403") ||
		strings.Contains(lower, "code=403") ||
		strings.Contains(lower, "code 403") {
		return true
	}
	for _, marker := range []string{
		"unauthorized",
		"forbidden",
		"invalid token",
		"token expired",
		"login required",
		"banned",
	} {
		if strings.Contains(lower, marker) {
			return true
		}
	}
	return false
}

func (app *App) recordStandardRequest(ctx context.Context, req StandardRequest) {
	markers := findLogTestMarkers(req.Prompt)
	testMarker := "-"
	if len(markers) > 0 {
		testMarker = strings.Join(markers, ",")
	}
	setRequestLogFields(ctx,
		"surface", req.Surface,
		"requested_model", req.ResponseModel,
		"resolved_model", req.ResolvedModel,
		"stream", boolLogValue(req.Stream),
		"tool_enabled", boolLogValue(req.ToolEnabled),
		"prompt_len", len(req.Prompt),
		"test_marker", testMarker,
	)
	app.logInfo(ctx, "标准请求解析完成",
		"model_mode", req.ModelMode,
		"chat_type", req.ChatType,
		"tools", strings.Join(req.ToolNames, ","),
		"thinking_forced", req.ForceThinking,
		"search", req.EnableSearch,
		"prompt_tail", promptTail(req.Prompt, 600),
		"prompt_sha256", promptSHA256(req.Prompt),
	)
	if req.ToolEnabled {
		app.logInfo(ctx, "[PromptSize]",
			"total", len(req.Prompt),
			"tools_part", len(toolPromptSection(req.Prompt)),
			"few_shot", strings.Count(req.Prompt, "[FEW-SHOT WARM-UP]"),
			"history", maxInt(len(req.Prompt)-len(toolPromptSection(req.Prompt)), 0),
			"latest", latestHumanLineLen(req.Prompt),
			"state_notice", strings.Count(req.Prompt, "[STATE NOTICE: MUST OBEY]"),
			"workspace", strings.Count(req.Prompt, "WORKSPACE"),
			"tool_related", true,
			"tool_count", len(req.Tools),
		)
	}
	if len(markers) > 0 {
		app.logInfo(ctx, "测试提示词追踪",
			"marker", testMarker,
			"prompt_tail", promptTail(req.Prompt, 240),
			"prompt_sha256", promptSHA256(req.Prompt),
		)
	}
}

func (app *App) handleChatCompletions(w http.ResponseWriter, r *http.Request) {
	auth, ok := app.resolveAuth(w, r)
	if !ok {
		return
	}
	var body map[string]any
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, map[string]any{"error": map[string]any{"message": "Invalid JSON body", "type": "invalid_request_error"}})
		return
	}
	req, err := app.prepareStandardRequest(r.Context(), r, body, "gpt-3.5-turbo", "openai", auth.Token)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	app.recordStandardRequest(r.Context(), req)
	id := "chatcmpl-" + randomID()[:12]
	created := time.Now().Unix()
	if req.Stream {
		app.streamOpenAI(w, r, req, id, created)
		return
	}
	result, err := app.runCompletion(r.Context(), req, "")
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, buildOpenAICompletionPayload(id, created, req, result))
}

func (app *App) streamOpenAI(w http.ResponseWriter, r *http.Request, req StandardRequest, id string, created int64) {
	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	if shouldBufferStreamTextDeltas(req) {
		app.streamOpenAIBuffered(w, r, req, id, created)
		return
	}
	flusher, _ := w.(http.Flusher)
	_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{"role": "assistant"}, nil)))
	if flusher != nil {
		flusher.Flush()
	}
	toolCallsSent := false
	result, err := app.runCompletionWithHooks(r.Context(), req, "", &completionStreamHooks{
		OnReasoningDelta: func(delta string) error {
			if delta == "" || toolCallsSent {
				return nil
			}
			_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{"reasoning_content": delta}, nil)))
			if flusher != nil {
				flusher.Flush()
			}
			return nil
		},
		OnAnswerDelta: func(delta string) error {
			if delta == "" || toolCallsSent {
				return nil
			}
			_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{"content": delta}, nil)))
			if flusher != nil {
				flusher.Flush()
			}
			return nil
		},
		OnToolCalls: func(calls []ParsedToolCall) error {
			if toolCallsSent {
				return nil
			}
			app.logParsedToolCalls(r.Context(), "ToolCall", "openai_stream_response", calls)
			app.logInfo(r.Context(), "[ToolDirective]", "tool_blocks", len(calls), "raw_tool_blocks", len(calls), "stop_reason", "tool_calls", "has_tool_use", true)
			for idx, call := range openAIToolCalls(calls) {
				_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{"tool_calls": []map[string]any{{
					"index":    idx,
					"id":       call["id"],
					"type":     call["type"],
					"function": call["function"],
				}}}, nil)))
			}
			_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{}, "tool_calls")))
			_, _ = w.Write([]byte("data: [DONE]\n\n"))
			if flusher != nil {
				flusher.Flush()
			}
			toolCallsSent = true
			return nil
		},
	})
	if err != nil {
		_, _ = w.Write([]byte("data: " + mustJSON(map[string]any{"error": err.Error()}) + "\n\n"))
		return
	}
	if toolCallsSent {
		return
	}
	if calls := completionToolCalls(result, req.Tools); len(calls) > 0 {
		app.logParsedToolCalls(r.Context(), "ToolCall", "openai_stream_response", calls)
		app.logInfo(r.Context(), "[ToolDirective]", "tool_blocks", len(calls), "raw_tool_blocks", len(calls), "stop_reason", "tool_calls", "has_tool_use", true)
		for idx, call := range openAIToolCalls(calls) {
			_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{"tool_calls": []map[string]any{{
				"index":    idx,
				"id":       call["id"],
				"type":     call["type"],
				"function": call["function"],
			}}}, nil)))
		}
		_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{}, "tool_calls")))
		_, _ = w.Write([]byte("data: [DONE]\n\n"))
		if flusher != nil {
			flusher.Flush()
		}
		return
	}
	if result.AnswerText == "" && result.ReasoningText == "" {
		fallback := emptyCompletionFallback(req, CompletionResult{FinishReason: "empty"})
		_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{"content": fallback}, nil)))
	}
	_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{}, "stop")))
	_, _ = w.Write([]byte("data: [DONE]\n\n"))
	if flusher != nil {
		flusher.Flush()
	}
}

func (app *App) streamOpenAIBuffered(w http.ResponseWriter, r *http.Request, req StandardRequest, id string, created int64) {
	flusher, _ := w.(http.Flusher)
	toolCallsSent := false
	_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{"role": "assistant"}, nil)))
	if flusher != nil {
		flusher.Flush()
	}
	result, err := app.runCompletionWithHooks(r.Context(), req, "", &completionStreamHooks{
		OnToolCalls: func(calls []ParsedToolCall) error {
			if toolCallsSent {
				return nil
			}
			app.logParsedToolCalls(r.Context(), "ToolCall", "openai_stream_response", calls)
			app.logInfo(r.Context(), "[ToolDirective]", "tool_blocks", len(calls), "raw_tool_blocks", len(calls), "stop_reason", "tool_calls", "has_tool_use", true)
			for idx, call := range openAIToolCalls(calls) {
				_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{"tool_calls": []map[string]any{{
					"index":    idx,
					"id":       call["id"],
					"type":     call["type"],
					"function": call["function"],
				}}}, nil)))
			}
			_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{}, "tool_calls")))
			_, _ = w.Write([]byte("data: [DONE]\n\n"))
			if flusher != nil {
				flusher.Flush()
			}
			toolCallsSent = true
			return nil
		},
	})
	if err != nil {
		_, _ = w.Write([]byte("data: " + mustJSON(map[string]any{"error": err.Error()}) + "\n\n"))
		return
	}
	if toolCallsSent {
		return
	}
	if calls := completionToolCalls(result, req.Tools); len(calls) > 0 {
		app.logParsedToolCalls(r.Context(), "ToolCall", "openai_stream_response", calls)
		app.logInfo(r.Context(), "[ToolDirective]", "tool_blocks", len(calls), "raw_tool_blocks", len(calls), "stop_reason", "tool_calls", "has_tool_use", true)
		for idx, call := range openAIToolCalls(calls) {
			_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{"tool_calls": []map[string]any{{
				"index":    idx,
				"id":       call["id"],
				"type":     call["type"],
				"function": call["function"],
			}}}, nil)))
		}
		_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{}, "tool_calls")))
	} else {
		if result.ReasoningText != "" {
			_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{"reasoning_content": result.ReasoningText}, nil)))
		}
		if result.AnswerText != "" {
			_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{"content": result.AnswerText}, nil)))
		}
		if result.AnswerText == "" && result.ReasoningText == "" {
			_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{"content": emptyCompletionFallback(req, result)}, nil)))
		}
		app.logInfo(r.Context(), "[ToolDirective]", "tool_blocks", 1, "raw_tool_blocks", 1, "stop_reason", "stop", "has_tool_use", false)
		_, _ = w.Write([]byte(openAIChunk(id, created, req.ResponseModel, map[string]any{}, "stop")))
	}
	_, _ = w.Write([]byte("data: [DONE]\n\n"))
	if flusher != nil {
		flusher.Flush()
	}
}

func (app *App) handleListModels(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.resolveAuth(w, r); !ok {
		return
	}
	upstream, err := app.client.ListModelsFromPool(r.Context())
	if err == nil && len(upstream) > 0 {
		writeJSON(w, http.StatusOK, buildOpenAIModelList(upstream))
		return
	}
	writeJSON(w, http.StatusOK, buildFallbackModelList())
}

func (app *App) handleGetModel(w http.ResponseWriter, r *http.Request) {
	modelID := r.PathValue("model_id")
	mode := parseModelMode(modelID, "")
	if mode.BaseModel == "" {
		writeError(w, http.StatusNotFound, map[string]any{"error": map[string]any{"message": "Model '" + modelID + "' not found", "type": "invalid_request_error"}})
		return
	}
	caps := map[string]bool{}
	switch mode.Mode {
	case "thinking":
		caps["thinking"] = true
	case "deep_research":
		caps["deep_research"] = true
		caps["search"] = true
	case "search":
		caps["search"] = true
	case "image":
		caps["image_gen"] = true
	case "video":
		caps["video_gen"] = true
	case "webdev":
		caps["web_dev"] = true
	case "slides":
		caps["slides"] = true
	}
	resolved := resolveModel(mode.BaseModel)
	payload := buildModelEntry(modelID, mode.BaseModel, caps, mode.Mode, modelID, strings.SplitN(resolved, "-", 2)[0], 0, "qwen2api")
	payload["resolved_model"] = resolved
	writeJSON(w, http.StatusOK, payload)
}

func (app *App) handleEmbeddings(w http.ResponseWriter, r *http.Request) {
	auth, ok := app.resolveAuth(w, r)
	if !ok {
		return
	}
	var body map[string]any
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	model := stringValue(body, "model", "text-embedding-ada-002")
	input := body["input"]
	inputs := []string{}
	switch v := input.(type) {
	case string:
		inputs = append(inputs, v)
	case []any:
		for _, item := range v {
			inputs = append(inputs, fmt.Sprint(item))
		}
	default:
		inputs = append(inputs, fmt.Sprint(v))
	}
	data := []map[string]any{}
	total := 0
	for i, text := range inputs {
		total += len(text)
		data = append(data, map[string]any{"object": "embedding", "embedding": pseudoEmbedding(text), "index": i})
	}
	app.addUsedTokens(auth.Token, total)
	writeJSON(w, http.StatusOK, map[string]any{
		"object": "list",
		"data":   data,
		"model":  model,
		"usage":  map[string]any{"prompt_tokens": total, "total_tokens": total},
	})
}

func (app *App) addUsedTokens(token string, delta int) {
	if delta <= 0 {
		return
	}
	var users []map[string]any
	if app.usersStore.LoadInto(&users) != nil {
		return
	}
	for _, user := range users {
		if stringValue(user, "id", "") == token {
			user["used_tokens"] = intValue(user, "used_tokens", 0) + delta
			break
		}
	}
	_ = app.usersStore.Save(users)
}

func (app *App) handleResponses(w http.ResponseWriter, r *http.Request) {
	auth, ok := app.resolveAuth(w, r)
	if !ok {
		return
	}
	var body map[string]any
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	converted := responsesToChatBody(body)
	req, err := app.prepareStandardRequest(r.Context(), r, converted, "gpt-3.5-turbo", "responses", auth.Token)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	app.recordStandardRequest(r.Context(), req)
	id := "resp_" + randomID()[:24]
	if req.Stream {
		app.streamResponses(w, r, req, id)
		return
	}
	result, err := app.runCompletion(r.Context(), req, "")
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	output := []map[string]any{}
	outputText := result.AnswerText
	if calls := completionToolCalls(result, req.Tools); len(calls) > 0 {
		output = responsesToolItems(calls)
		outputText = ""
	} else {
		if outputText == "" && result.ReasoningText == "" {
			outputText = emptyCompletionFallback(req, result)
		}
		content := []map[string]any{{"type": "output_text", "text": outputText, "annotations": []any{}}}
		if result.ReasoningText != "" {
			content = append([]map[string]any{{"type": "reasoning_text", "text": result.ReasoningText}}, content...)
		}
		output = append(output, map[string]any{
			"id": "msg_" + randomID()[:12], "type": "message", "status": "completed", "role": "assistant",
			"content": content,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"id": id, "object": "response", "created_at": time.Now().Unix(), "status": "completed", "model": req.ResponseModel,
		"output": output, "parallel_tool_calls": true, "error": nil, "incomplete_details": nil,
		"output_text": outputText,
		"usage":       map[string]any{"input_tokens": len(req.Prompt), "output_tokens": len(result.AnswerText), "total_tokens": len(req.Prompt) + len(result.AnswerText)},
	})
}

func responsesToChatBody(body map[string]any) map[string]any {
	messages := []any{}
	if instructions := responseStringifyContent(body["instructions"]); strings.TrimSpace(instructions) != "" {
		messages = append(messages, map[string]any{"role": "system", "content": instructions})
	}
	input, hasInput := body["input"]
	if hasInput {
		messages = append(messages, responsesInputMessages(input)...)
	}
	if len(messages) == 0 {
		messages = anyList(body["messages"])
	}
	if len(messages) == 0 {
		messages = append(messages, map[string]any{"role": "user", "content": ""})
	}
	out := map[string]any{
		"model": body["model"], "messages": messages, "stream": body["stream"],
		"tools":           normalizeResponsesTools(anyList(body["tools"])),
		"enable_thinking": body["enable_thinking"],
	}
	for _, key := range []string{"session_key", "conversation_id", "_workspace_root", "upstream_files", "store"} {
		if v, ok := body[key]; ok {
			out[key] = v
		}
	}
	if v, ok := body["previous_response_id"]; ok && out["session_key"] == nil {
		out["session_key"] = v
	}
	return out
}

func responsesInputMessages(input any) []any {
	switch v := input.(type) {
	case string:
		return []any{map[string]any{"role": "user", "content": v}}
	case []any:
		out := []any{}
		for _, item := range v {
			switch x := item.(type) {
			case string:
				out = append(out, map[string]any{"role": "user", "content": x})
			case map[string]any:
				itemType := stringValue(x, "type", "")
				if itemType == "message" || x["role"] != nil {
					out = append(out, convertResponseMessage(x))
					continue
				}
				if responseToolCallTypes[itemType] {
					out = append(out, convertResponseToolCallItem(x))
					continue
				}
				if responseToolOutputTypes[itemType] {
					out = append(out, convertResponseToolOutputItem(x))
					continue
				}
				out = append(out, map[string]any{"role": "user", "content": convertResponseMessageContent([]any{x}, "user")})
			default:
				out = append(out, map[string]any{"role": "user", "content": fmt.Sprint(item)})
			}
		}
		return out
	default:
		return []any{map[string]any{"role": "user", "content": fmt.Sprint(v)}}
	}
}

var (
	responseToolCallTypes   = map[string]bool{"function_call": true, "custom_tool_call": true, "local_shell_call": true, "shell_call": true}
	responseToolOutputTypes = map[string]bool{"function_call_output": true, "custom_tool_call_output": true, "local_shell_call_output": true, "shell_call_output": true}
)

func convertResponseMessage(item map[string]any) map[string]any {
	role := stringValue(item, "role", "user")
	if role == "developer" {
		role = "system"
	}
	if !map[string]bool{"system": true, "user": true, "assistant": true, "tool": true}[role] {
		role = "user"
	}
	content := item["content"]
	if content == nil {
		content = item["input"]
	}
	msg := map[string]any{"role": role, "content": convertResponseMessageContent(content, role)}
	if calls, ok := item["tool_calls"].([]any); ok {
		msg["tool_calls"] = calls
	}
	if id := stringValue(item, "tool_call_id", ""); id != "" {
		msg["tool_call_id"] = id
	}
	return msg
}

func convertResponseMessageContent(content any, role string) any {
	switch v := content.(type) {
	case nil:
		return ""
	case string:
		return v
	case []any:
		converted := []any{}
		textParts := []string{}
		hasNonText := false
		for _, part := range v {
			c := convertResponseContentPart(part, role)
			if m, ok := c.(map[string]any); ok && stringValue(m, "type", "") == "text" {
				if text := stringValue(m, "text", ""); text != "" {
					textParts = append(textParts, text)
				}
			} else {
				hasNonText = true
			}
			converted = append(converted, c)
		}
		if hasNonText {
			return converted
		}
		return strings.Join(textParts, "\n")
	case map[string]any:
		c := convertResponseContentPart(v, role)
		if m, ok := c.(map[string]any); ok && stringValue(m, "type", "") == "text" {
			return stringValue(m, "text", "")
		}
		return []any{c}
	default:
		return fmt.Sprint(v)
	}
}

func convertResponseContentPart(part any, role string) any {
	switch v := part.(type) {
	case string:
		return map[string]any{"type": "text", "text": v}
	case map[string]any:
		partType := stringValue(v, "type", "")
		switch partType {
		case "input_text", "output_text", "text":
			return map[string]any{"type": "text", "text": stringValue(v, "text", "")}
		case "input_image", "image_url":
			if imageURL, ok := v["image_url"].(map[string]any); ok {
				return map[string]any{"type": "image_url", "image_url": imageURL}
			}
			if imageURL := stringValue(v, "image_url", ""); imageURL != "" {
				return map[string]any{"type": "image_url", "image_url": map[string]any{"url": imageURL}}
			}
			return v
		case "input_file", "file":
			out := copyMap(v)
			out["type"] = "input_file"
			if out["filename"] == nil && out["name"] != nil {
				out["filename"] = out["name"]
			}
			if out["data_base64"] == nil && out["file_data"] != nil {
				out["data_base64"] = out["file_data"]
			}
			return out
		case "tool_result", "function_call_output":
			return map[string]any{"type": "tool_result", "tool_use_id": firstString(v["tool_use_id"], v["call_id"], v["id"]), "content": responseStringifyContent(firstNonNil(v["content"], v["output"]))}
		case "refusal":
			return map[string]any{"type": "text", "text": firstString(v["refusal"], v["text"])}
		}
		if role == "assistant" && stringValue(v, "text", "") != "" {
			return map[string]any{"type": "text", "text": stringValue(v, "text", "")}
		}
		if stringValue(v, "content", "") != "" {
			return map[string]any{"type": "text", "text": stringValue(v, "content", "")}
		}
		return map[string]any{"type": "text", "text": mustJSON(v)}
	default:
		return map[string]any{"type": "text", "text": fmt.Sprint(v)}
	}
}

func convertResponseToolCallItem(item map[string]any) map[string]any {
	callID := firstNonEmpty(firstString(item["call_id"], item["id"]), "call_"+randomID()[:12])
	name := responseToolItemName(item)
	arguments := responseToolItemArguments(item)
	if _, ok := arguments.(string); !ok {
		arguments = mustJSON(firstNonNil(arguments, map[string]any{}))
	}
	return map[string]any{
		"role": "assistant", "content": nil,
		"tool_calls": []map[string]any{{"id": callID, "type": "function", "function": map[string]any{"name": name, "arguments": arguments}}},
	}
}

func convertResponseToolOutputItem(item map[string]any) map[string]any {
	return map[string]any{
		"role": "tool", "tool_call_id": firstString(item["call_id"], item["id"]),
		"content": responseStringifyContent(firstNonNil(item["output"], item["content"])),
	}
}

func responseToolItemName(item map[string]any) string {
	itemType := stringValue(item, "type", "")
	switch itemType {
	case "local_shell_call":
		return "local_shell"
	case "shell_call":
		return "shell"
	case "custom_tool_call":
		return firstNonEmpty(stringValue(item, "name", ""), "custom_tool")
	default:
		return stringValue(item, "name", "")
	}
}

func responseToolItemArguments(item map[string]any) any {
	itemType := stringValue(item, "type", "")
	if itemType == "local_shell_call" || itemType == "shell_call" {
		action, _ := item["action"].(map[string]any)
		return map[string]any{
			"command":           firstNonNil(action["command"], item["command"], item["input"], ""),
			"timeout_ms":        firstNonNil(action["timeout_ms"], item["timeout_ms"]),
			"working_directory": firstNonNil(action["working_directory"], item["working_directory"]),
			"env":               firstNonNil(action["env"], item["env"]),
		}
	}
	if itemType == "custom_tool_call" {
		return firstNonNil(item["input"], item["arguments"], "")
	}
	return firstNonNil(item["arguments"], item["input"], map[string]any{})
}

func normalizeResponsesTools(tools []any) []any {
	out := []any{}
	for _, raw := range tools {
		tool, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if normalized := normalizeResponsesTool(tool); normalized != nil {
			out = append(out, normalized)
		}
	}
	return out
}

func normalizeResponsesTool(tool map[string]any) map[string]any {
	toolType := stringValue(tool, "type", "")
	if toolType == "shell" || toolType == "local_shell" {
		return map[string]any{"type": "function", "function": map[string]any{
			"name":        toolType,
			"description": firstNonEmpty(stringValue(tool, "description", ""), "Run a "+toolType+" command."),
			"parameters":  map[string]any{"type": "object", "properties": map[string]any{"command": map[string]any{"type": "string"}, "timeout_ms": map[string]any{"type": "integer"}, "working_directory": map[string]any{"type": "string"}, "env": map[string]any{"type": "object"}}, "required": []string{"command"}},
		}}
	}
	if toolType == "custom" || toolType == "custom_tool" {
		name := firstNonEmpty(stringValue(tool, "name", ""), "custom_tool")
		params, _ := tool["parameters"].(map[string]any)
		if params == nil {
			params = map[string]any{"type": "object", "properties": map[string]any{"input": map[string]any{"type": "string"}}, "required": []string{"input"}}
		}
		return map[string]any{"type": "function", "function": map[string]any{"name": name, "description": firstNonEmpty(stringValue(tool, "description", ""), "Custom text tool."), "parameters": params}}
	}
	if toolType == "function" {
		fn, _ := tool["function"].(map[string]any)
		if fn == nil || stringValue(fn, "name", "") == "" {
			return nil
		}
		params := firstNonNil(fn["parameters"], fn["input_schema"], tool["parameters"], map[string]any{})
		return map[string]any{"type": "function", "function": map[string]any{"name": stringValue(fn, "name", ""), "description": firstNonEmpty(stringValue(fn, "description", ""), stringValue(tool, "description", "")), "parameters": params}}
	}
	name := stringValue(tool, "name", "")
	if name == "" {
		return nil
	}
	return map[string]any{"type": "function", "function": map[string]any{"name": name, "description": stringValue(tool, "description", ""), "parameters": firstNonNil(tool["parameters"], tool["input_schema"], map[string]any{})}}
}

func responseStringifyContent(value any) string {
	switch v := value.(type) {
	case nil:
		return ""
	case string:
		return v
	case []any:
		parts := []string{}
		for _, item := range v {
			if text := responseStringifyContent(item); text != "" {
				parts = append(parts, text)
			}
		}
		return strings.Join(parts, "\n")
	case map[string]any:
		for _, key := range []string{"text", "output", "content"} {
			if text := responseStringifyContent(v[key]); text != "" {
				return text
			}
		}
		return mustJSON(v)
	default:
		return fmt.Sprint(v)
	}
}

func copyMap(src map[string]any) map[string]any {
	out := map[string]any{}
	for k, v := range src {
		out[k] = v
	}
	return out
}

func firstNonNil(values ...any) any {
	for _, value := range values {
		if value != nil {
			return value
		}
	}
	return nil
}

func (app *App) streamResponses(w http.ResponseWriter, r *http.Request, req StandardRequest, id string) {
	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	writeSSEEvent(w, "response.created", map[string]any{"type": "response.created", "response": map[string]any{"id": id, "status": "in_progress"}})
	flushSSE(w)

	toolCallsSent := false
	hooks := &completionStreamHooks{
		OnAnswerDelta: func(delta string) error {
			if delta == "" || toolCallsSent {
				return nil
			}
			writeSSEEvent(w, "response.output_text.delta", map[string]any{"type": "response.output_text.delta", "delta": delta})
			flushSSE(w)
			return nil
		},
		OnToolCalls: func(calls []ParsedToolCall) error {
			if !req.ToolEnabled || toolCallsSent {
				return nil
			}
			app.logParsedToolCalls(r.Context(), "ToolCall", "responses_stream_response", calls)
			app.logInfo(r.Context(), "[ToolDirective]", "tool_blocks", len(calls), "raw_tool_blocks", len(calls), "stop_reason", "tool_calls", "has_tool_use", true)
			output := responsesToolItems(calls)
			for _, item := range output {
				writeSSEEvent(w, "response.output_item.done", map[string]any{"type": "response.output_item.done", "item": item})
			}
			writeSSEEvent(w, "response.completed", map[string]any{
				"type":     "response.completed",
				"response": map[string]any{"id": id, "status": "completed", "output": output, "output_text": ""},
			})
			flushSSE(w)
			toolCallsSent = true
			return nil
		},
	}

	result, err := app.runCompletionWithHooks(r.Context(), req, "", hooks)
	if err != nil {
		writeSSEEvent(w, "error", map[string]any{"type": "error", "error": err.Error()})
		flushSSE(w)
		return
	}
	if toolCallsSent {
		return
	}
	output := []map[string]any{}
	outputText := result.AnswerText
	if calls := completionToolCalls(result, req.Tools); len(calls) > 0 {
		app.logParsedToolCalls(r.Context(), "ToolCall", "responses_stream_response", calls)
		app.logInfo(r.Context(), "[ToolDirective]", "tool_blocks", len(calls), "raw_tool_blocks", len(calls), "stop_reason", "tool_calls", "has_tool_use", true)
		output = responsesToolItems(calls)
		outputText = ""
		for _, item := range output {
			writeSSEEvent(w, "response.output_item.done", map[string]any{"type": "response.output_item.done", "item": item})
		}
	} else {
		app.logInfo(r.Context(), "[ToolDirective]", "tool_blocks", 1, "raw_tool_blocks", 1, "stop_reason", "completed", "has_tool_use", false)
		if outputText == "" && result.ReasoningText == "" {
			outputText = emptyCompletionFallback(req, result)
		}
		output = append(output, map[string]any{"type": "message", "role": "assistant", "content": []map[string]any{{"type": "output_text", "text": outputText}}})
		if req.ToolEnabled || result.AnswerText == "" {
			writeSSEEvent(w, "response.output_text.delta", map[string]any{"type": "response.output_text.delta", "delta": outputText})
		}
	}
	writeSSEEvent(w, "response.completed", map[string]any{"type": "response.completed", "response": map[string]any{"id": id, "status": "completed", "output": output, "output_text": outputText}})
	flushSSE(w)
}

func (app *App) handleAnthropicCountTokens(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.resolveAuth(w, r); !ok {
		return
	}
	var body map[string]any
	_ = decodeJSON(r, &body)
	prompt := anthropicPrompt(body)
	writeJSON(w, http.StatusOK, map[string]any{"input_tokens": len(prompt)})
}

func (app *App) handleAnthropicMessages(w http.ResponseWriter, r *http.Request) {
	auth, ok := app.resolveAuth(w, r)
	if !ok {
		return
	}
	var body map[string]any
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	chatBody := map[string]any{"model": body["model"], "messages": anthropicMessages(body), "stream": body["stream"], "tools": body["tools"]}
	for _, key := range []string{"session_key", "conversation_id", "_workspace_root", "upstream_files", "store"} {
		if value, ok := body[key]; ok {
			chatBody[key] = value
		}
	}
	req, err := app.prepareStandardRequest(r.Context(), r, chatBody, "claude-3-haiku", "anthropic", auth.Token)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	app.recordStandardRequest(r.Context(), req)
	app.logInfo(r.Context(), "[ANT]",
		"model", req.ResolvedModel,
		"stream", req.Stream,
		"tool_enabled", req.ToolEnabled,
		"tools", strings.Join(req.ToolNames, ","),
		"prompt_len", len(req.Prompt),
		"prompt_tail", promptTail(req.Prompt, 600),
	)
	id := "msg_" + randomID()[:24]
	if req.Stream {
		app.streamAnthropic(w, r, req, id)
		return
	}
	result, err := app.runCompletion(r.Context(), req, "")
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, app.buildAnthropicPayload(r.Context(), id, req.ResponseModel, req.Prompt, req, result, "json_response"))
}

func anthropicMessages(body map[string]any) []any {
	out := []any{}
	if system := body["system"]; system != nil {
		out = append(out, map[string]any{"role": "system", "content": system})
	}
	out = append(out, anyList(body["messages"])...)
	return out
}

func anthropicPrompt(body map[string]any) string {
	chatBody := map[string]any{"messages": anthropicMessages(body), "tools": body["tools"]}
	prompt, _ := adapter.MessagesToPrompt(chatBody)
	return prompt
}

func (app *App) streamAnthropic(w http.ResponseWriter, r *http.Request, req StandardRequest, id string) {
	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	writeSSEEvent(w, "message_start", map[string]any{
		"type": "message_start",
		"message": map[string]any{
			"id": id, "type": "message", "role": "assistant", "content": []any{}, "model": req.ResponseModel, "stop_reason": nil,
			"usage": map[string]any{"input_tokens": len(req.Prompt)},
		},
	})
	flushSSE(w)

	activeBlockType := ""
	activeBlockIndex := 0
	nextBlockIndex := 0
	emittedContent := false
	messageStopped := false
	startBlock := func(kind string) {
		if activeBlockType == kind {
			return
		}
		if activeBlockType != "" {
			writeSSEEvent(w, "content_block_stop", map[string]any{"type": "content_block_stop", "index": activeBlockIndex})
			nextBlockIndex++
		}
		activeBlockType = kind
		activeBlockIndex = nextBlockIndex
		if kind == "thinking" {
			writeSSEEvent(w, "content_block_start", map[string]any{"type": "content_block_start", "index": activeBlockIndex, "content_block": map[string]any{"type": "thinking", "thinking": ""}})
		} else {
			writeSSEEvent(w, "content_block_start", map[string]any{"type": "content_block_start", "index": activeBlockIndex, "content_block": map[string]any{"type": "text", "text": ""}})
		}
		emittedContent = true
	}
	stopMessage := func(stopReason string, outputTokens int) {
		if activeBlockType != "" {
			writeSSEEvent(w, "content_block_stop", map[string]any{"type": "content_block_stop", "index": activeBlockIndex})
			activeBlockType = ""
			nextBlockIndex = activeBlockIndex + 1
		}
		if messageStopped {
			return
		}
		writeSSEEvent(w, "message_delta", map[string]any{"type": "message_delta", "delta": map[string]any{"stop_reason": stopReason}, "usage": map[string]any{"output_tokens": outputTokens}})
		writeSSEEvent(w, "message_stop", map[string]any{"type": "message_stop"})
		flushSSE(w)
		messageStopped = true
	}

	toolCallsSent := false
	hooks := &completionStreamHooks{
		OnReasoningDelta: func(delta string) error {
			if delta == "" || toolCallsSent {
				return nil
			}
			startBlock("thinking")
			writeSSEEvent(w, "content_block_delta", map[string]any{"type": "content_block_delta", "index": activeBlockIndex, "delta": map[string]any{"type": "thinking_delta", "thinking": delta}})
			flushSSE(w)
			return nil
		},
		OnAnswerDelta: func(delta string) error {
			if delta == "" || toolCallsSent {
				return nil
			}
			startBlock("text")
			writeSSEEvent(w, "content_block_delta", map[string]any{"type": "content_block_delta", "index": activeBlockIndex, "delta": map[string]any{"type": "text_delta", "text": delta}})
			flushSSE(w)
			return nil
		},
		OnToolCalls: func(calls []ParsedToolCall) error {
			if !req.ToolEnabled || toolCallsSent {
				return nil
			}
			app.logParsedToolCalls(r.Context(), "ToolCall", "stream_response", calls)
			app.logInfo(r.Context(), "[ToolDirective]", "tool_blocks", len(calls), "raw_tool_blocks", len(calls), "stop_reason", "tool_use", "has_tool_use", true)
			app.logParsedToolCalls(r.Context(), "ANT-ToolOut", "stream_response", calls)
			writeAnthropicToolUseEvents(w, calls, 0)
			toolCallsSent = true
			emittedContent = true
			stopMessage("tool_use", 0)
			return nil
		},
	}

	result, err := app.runCompletionWithHooks(r.Context(), req, "", hooks)
	if err != nil {
		writeSSEEvent(w, "error", map[string]any{"type": "error", "error": err.Error()})
		flushSSE(w)
		return
	}
	if toolCallsSent {
		return
	}
	if !req.ToolEnabled {
		stopReason := "end_turn"
		outputTokens := len(result.AnswerText)
		if !emittedContent {
			content, finalStopReason, finalOutputTokens := app.anthropicContentBlocks(r.Context(), req, result, "stream_response")
			for index, block := range content {
				switch stringValue(block, "type", "") {
				case "thinking":
					writeSSEEvent(w, "content_block_start", map[string]any{"type": "content_block_start", "index": index, "content_block": map[string]any{"type": "thinking", "thinking": ""}})
					writeSSEEvent(w, "content_block_delta", map[string]any{"type": "content_block_delta", "index": index, "delta": map[string]any{"type": "thinking_delta", "thinking": stringValue(block, "thinking", "")}})
					writeSSEEvent(w, "content_block_stop", map[string]any{"type": "content_block_stop", "index": index})
				default:
					writeSSEEvent(w, "content_block_start", map[string]any{"type": "content_block_start", "index": index, "content_block": map[string]any{"type": "text", "text": ""}})
					writeSSEEvent(w, "content_block_delta", map[string]any{"type": "content_block_delta", "index": index, "delta": map[string]any{"type": "text_delta", "text": stringValue(block, "text", "")}})
					writeSSEEvent(w, "content_block_stop", map[string]any{"type": "content_block_stop", "index": index})
				}
			}
			stopReason = finalStopReason
			outputTokens = finalOutputTokens
		}
		stopMessage(stopReason, outputTokens)
		return
	}
	content, stopReason, outputTokens := app.anthropicContentBlocks(r.Context(), req, result, "stream_response")
	toolCalls := completionToolCalls(result, req.Tools)
	if len(toolCalls) > 0 {
		writeAnthropicToolUseEvents(w, toolCalls, 0)
		stopMessage(stopReason, outputTokens)
		return
	}
	for index, block := range content {
		switch stringValue(block, "type", "") {
		case "thinking":
			writeSSEEvent(w, "content_block_start", map[string]any{"type": "content_block_start", "index": index, "content_block": map[string]any{"type": "thinking", "thinking": ""}})
			writeSSEEvent(w, "content_block_delta", map[string]any{"type": "content_block_delta", "index": index, "delta": map[string]any{"type": "thinking_delta", "thinking": stringValue(block, "thinking", "")}})
			writeSSEEvent(w, "content_block_stop", map[string]any{"type": "content_block_stop", "index": index})
		default:
			writeSSEEvent(w, "content_block_start", map[string]any{"type": "content_block_start", "index": index, "content_block": map[string]any{"type": "text", "text": ""}})
			writeSSEEvent(w, "content_block_delta", map[string]any{"type": "content_block_delta", "index": index, "delta": map[string]any{"type": "text_delta", "text": stringValue(block, "text", "")}})
			writeSSEEvent(w, "content_block_stop", map[string]any{"type": "content_block_stop", "index": index})
		}
	}
	stopMessage(stopReason, outputTokens)
}

func (app *App) handleGeminiGenerate(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.resolveAuth(w, r); !ok {
		return
	}
	var body map[string]any
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	model := geminiModelFromPath(r.URL.Path)
	req := buildChatStandardRequest(geminiToChatBody(model, body, false), model, "gemini")
	app.recordStandardRequest(r.Context(), req)
	result, err := app.runCompletion(r.Context(), req, "")
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, geminiPayload(result.AnswerText))
}

func (app *App) handleGeminiStream(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.resolveAuth(w, r); !ok {
		return
	}
	var body map[string]any
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	model := geminiModelFromPath(r.URL.Path)
	req := buildChatStandardRequest(geminiToChatBody(model, body, true), model, "gemini")
	app.recordStandardRequest(r.Context(), req)
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	result, err := app.runCompletion(r.Context(), req, "")
	if err != nil {
		_, _ = w.Write([]byte(mustJSON(map[string]any{"error": err.Error()}) + "\n"))
		return
	}
	_, _ = w.Write([]byte(mustJSON(geminiPayload(result.AnswerText)) + "\n"))
}

func geminiModelFromPath(path string) string {
	if idx := strings.IndexByte(path, '?'); idx >= 0 {
		path = path[:idx]
	}
	tail := path
	for _, prefix := range []string{"/v1beta/models/", "/v1/models/", "/models/"} {
		if strings.HasPrefix(path, prefix) {
			tail = strings.TrimPrefix(path, prefix)
			break
		}
	}
	if idx := strings.IndexByte(tail, ':'); idx >= 0 {
		tail = tail[:idx]
	}
	if decoded, err := url.PathUnescape(tail); err == nil {
		return decoded
	}
	return tail
}

func geminiToChatBody(model string, body map[string]any, stream bool) map[string]any {
	messages := []any{}
	for _, raw := range anyList(body["contents"]) {
		m, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		role := stringValue(m, "role", "user")
		if role == "model" {
			role = "assistant"
		}
		var parts []any
		for _, part := range anyList(m["parts"]) {
			pm, ok := part.(map[string]any)
			if ok && pm["text"] != nil {
				parts = append(parts, map[string]any{"type": "text", "text": stringValue(pm, "text", "")})
			}
		}
		messages = append(messages, map[string]any{"role": role, "content": parts})
	}
	return map[string]any{"model": model, "messages": messages, "stream": stream}
}

func geminiPayload(text string) map[string]any {
	return map[string]any{"candidates": []map[string]any{{"content": map[string]any{"parts": []map[string]any{{"text": text}}, "role": "model"}}}}
}

func (app *App) handleUploadFile(w http.ResponseWriter, r *http.Request) {
	auth, ok := app.resolveAuth(w, r)
	if !ok {
		return
	}
	if err := r.ParseMultipartForm(256 << 20); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	file, header, err := r.FormFile("file")
	if err != nil {
		writeError(w, http.StatusBadRequest, "file is required")
		return
	}
	defer file.Close()
	ext := fileExt(header.Filename)
	if !splitExts(app.settings.ContextAllowedUserExts)[ext] {
		writeError(w, http.StatusBadRequest, "Unsupported file extension: "+ext)
		return
	}
	raw, err := io.ReadAll(io.LimitReader(file, 128<<20))
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if len(raw) == 0 {
		writeError(w, http.StatusBadRequest, "Empty file")
		return
	}
	contentType := header.Header.Get("Content-Type")
	if contentType == "" {
		contentType = mime.TypeByExtension("." + ext)
	}
	record, err := app.saveLocalBytes(header.Filename, contentType, raw, "upload", "user-upload", auth.Token, false)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"id": record.ID, "object": "file", "filename": record.Filename, "bytes": len(raw),
		"content_type": record.ContentType, "created_at": record.CreatedAt,
		"content_block": map[string]any{"type": "input_file", "file_id": record.ID, "filename": record.Filename, "mime_type": record.ContentType},
	})
}

func (app *App) handleDeleteFile(w http.ResponseWriter, r *http.Request) {
	auth, ok := app.resolveAuth(w, r)
	if !ok {
		return
	}
	fileID := r.PathValue("file_id")
	records, err := app.loadUploadedLocalFiles()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	next := records[:0]
	found := false
	for _, record := range records {
		if record.ID != fileID {
			next = append(next, record)
			continue
		}
		found = true
		if record.OwnerToken != "" && record.OwnerToken != auth.Token {
			writeError(w, http.StatusForbidden, "Forbidden")
			return
		}
		if record.Path != "" {
			_ = safeRemoveGeneratedPath(app.settings.ContextGeneratedDir, record.Path)
		}
	}
	if !found {
		writeError(w, http.StatusNotFound, "File not found")
		return
	}
	_ = app.saveUploadedLocalFiles(next)
	writeJSON(w, http.StatusOK, map[string]any{"deleted": true, "id": fileID})
}

func (app *App) handleImages(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.resolveAuth(w, r); !ok {
		return
	}
	var body map[string]any
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	prompt := strings.TrimSpace(stringValue(body, "prompt", ""))
	if prompt == "" {
		writeError(w, http.StatusBadRequest, "prompt is required")
		return
	}
	n := min(max(1, intValue(body, "n", 1)), 4)
	size, ratio := normalizeMediaSize(firstStringAny(body["size"], body["ratio"], body["aspect_ratio"]))
	width, height := mediaDimensions(size)
	model := resolveMediaModel(stringValue(body, "model", ""), true)
	setRequestLogFields(r.Context(), "surface", "images", "requested_model", stringValue(body, "model", ""), "resolved_model", model, "stream", "false", "tool_enabled", "false", "prompt_len", len(prompt))
	app.logInfo(r.Context(), "图片生成请求解析完成", "size", size, "ratio", ratio, "width", width, "height", height, "n", n)
	promptText := "请调用图片生成能力直接生成图片，不要只输出文字描述。如果可以生成图片，请返回可访问的图片链接或包含图片链接的结果。\n" +
		"强制画布尺寸：" + size + " 像素。强制宽高比：" + ratio + "。必须严格按这个尺寸和比例生成，不要裁切成其它比例，不要改成默认尺寸。\n\n用户需求：" + prompt

	urls, lastErr := app.createImageURLs(r.Context(), model, promptText, map[string]any{"size": size, "ratio": ratio, "width": width, "height": height})
	if lastErr != nil {
		app.logWarn(r.Context(), "图片生成失败", "error", lastErr)
		writeError(w, upstreamMediaErrorStatus(lastErr), lastErr.Error())
		return
	}
	data := []map[string]any{}
	for i, url := range urls {
		if i >= n {
			break
		}
		data = append(data, map[string]any{"url": url, "revised_prompt": prompt, "size": size, "ratio": ratio, "width": width, "height": height})
	}
	if len(data) == 0 {
		app.logWarn(r.Context(), "图片生成没有可返回的图片链接", "url_count", len(urls))
		writeError(w, http.StatusInternalServerError, "Image generation produced no image URL")
		return
	}
	app.logInfo(r.Context(), "图片生成完成", "returned", len(data))
	writeJSON(w, http.StatusOK, map[string]any{"created": time.Now().Unix(), "data": data})
}

func (app *App) createImageURLs(ctx context.Context, model, promptText string, imageOptions map[string]any) ([]string, error) {
	var lastErr error
	attempts := app.mediaRetryAttempts()
	for attempt := 0; attempt < attempts; attempt++ {
		acc, err := app.accounts.AcquireFor(ctx, "", accountUsageImage)
		if err != nil {
			app.logWarn(ctx, "图片生成获取账号失败", "attempt", attempt+1, "error", err)
			return nil, err
		}
		func() {
			defer app.accounts.Release(acc)
			setRequestLogFields(ctx, "account", acc.Email)
			app.logInfo(ctx, "图片生成开始尝试", "attempt", attempt+1, "model", model)

			chatID, err := app.client.CreateChat(ctx, qwenCreds(acc.Token, acc.Cookies), model, "image_gen")
			if err != nil {
				app.classifyAccountErrorFor(acc, err, accountUsageImage)
				lastErr = err
				app.logWarn(ctx, "图片生成创建会话失败", "attempt", attempt+1, "error", err)
				return
			}
			defer asyncDeleteChat(app.client, acc.Token, acc.Cookies, chatID)
			setRequestLogFields(ctx, "chat_id", chatID)

			payload := buildChatPayload(chatID, model, promptText, false, nil, "image_gen", imageOptions, nil, false)
			parts := []string{}
			if err := app.client.StreamChat(ctx, qwenCreds(acc.Token, acc.Cookies), chatID, payload, func(evt UpstreamEvent) error {
				if evt.Content != "" {
					parts = append(parts, evt.Content)
				}
				if evt.Raw != nil {
					parts = append(parts, mustJSON(evt.Raw))
				}
				return nil
			}); err != nil {
				app.classifyAccountErrorFor(acc, err, accountUsageImage)
				lastErr = err
				app.logWarn(ctx, "图片生成上游流式失败", "attempt", attempt+1, "error", err, "parts", len(parts))
				return
			}

			answerText := strings.Join(parts, "\n")
			if _, detail, err := app.client.GetChatDetail(ctx, qwenCreds(acc.Token, acc.Cookies), chatID, 30*time.Second); err == nil && detail != "" {
				answerText += "\n" + detail
			}
			if chats, err := app.client.ListChats(ctx, qwenCreds(acc.Token, acc.Cookies), 20); err == nil {
				for _, chat := range chats {
					if stringValue(chat, "id", "") == chatID {
						answerText += "\n" + mustJSON(chat)
						break
					}
				}
			}
			if failure := extractUpstreamFailure(answerText); failure != "" {
				lastErr = fmt.Errorf("%s", failure)
				app.classifyAccountErrorFor(acc, lastErr, accountUsageImage)
				app.logWarn(ctx, "图片生成上游返回失败", "attempt", attempt+1, "failure", failure)
				return
			}

			urls := extractImageURLs(answerText)
			app.logInfo(ctx, "图片生成链接提取完成", "attempt", attempt+1, "url_count", len(urls), "answer_len", len(answerText))
			if len(urls) == 0 {
				lastErr = fmt.Errorf("Image generation produced no image URL (chat_id=%s)", chatID)
				app.logWarn(ctx, "图片生成未识别到图片链接", "attempt", attempt+1, "answer_tail", promptTail(answerText, 240))
				return
			}
			app.accounts.MarkSuccessFor(acc, accountUsageImage)
			lastErr = nil
			imageOptions["urls"] = urls
			app.logInfo(ctx, "图片生成尝试成功", "attempt", attempt+1, "url_count", len(urls))
		}()
		if lastErr == nil {
			if urls, ok := imageOptions["urls"].([]string); ok {
				return urls, nil
			}
		}
		app.logWarn(ctx, "图片生成尝试失败", "attempt", attempt+1, "error", lastErr)
		if !app.accounts.HasAvailableFor(accountUsageImage) {
			app.logWarn(ctx, "图片生成账号池已无可用账号", "attempt", attempt+1, "error", lastErr)
			break
		}
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("image generation failed")
	}
	return nil, fmt.Errorf("All %d attempts failed. Last error: %w", attempts, lastErr)
}

func (app *App) handleVideos(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.resolveAuth(w, r); !ok {
		return
	}
	var body map[string]any
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	prompt := strings.TrimSpace(stringValue(body, "prompt", ""))
	if prompt == "" {
		writeError(w, http.StatusBadRequest, "prompt is required")
		return
	}
	n := min(max(1, intValue(body, "n", 1)), 2)
	size, ratio := normalizeMediaSize(firstStringAny(body["size"], body["ratio"], body["aspect_ratio"]))
	width, height := mediaDimensions(size)
	duration := min(max(1, intValue(body, "duration", 5)), 10)
	model := resolveMediaModel(stringValue(body, "model", ""), false)
	setRequestLogFields(r.Context(), "surface", "videos", "requested_model", stringValue(body, "model", ""), "resolved_model", model, "stream", "false", "tool_enabled", "false", "prompt_len", len(prompt))
	app.logInfo(r.Context(), "视频生成请求解析完成", "size", size, "ratio", ratio, "width", width, "height", height, "duration", duration, "n", n)
	promptText := fmt.Sprintf("%s\n\n视频要求：生成 %d 秒视频，宽高比 %s，参考画面尺寸 %s。", prompt, duration, ratio, size)
	urls, lastErr := app.createVideoURLs(r.Context(), model, promptText, map[string]any{"size": size, "ratio": ratio, "width": width, "height": height, "duration": duration})
	if lastErr != nil {
		app.logWarn(r.Context(), "视频生成失败", "error", lastErr)
		writeError(w, upstreamMediaErrorStatus(lastErr), lastErr.Error())
		return
	}
	data := []map[string]any{}
	for i, url := range urls {
		if i >= n {
			break
		}
		data = append(data, map[string]any{"url": url, "revised_prompt": prompt, "size": size, "ratio": ratio, "width": width, "height": height, "duration": duration})
	}
	if len(data) == 0 {
		app.logWarn(r.Context(), "视频生成未识别到视频链接")
		writeError(w, http.StatusInternalServerError, "Video generation produced no video URL")
		return
	}
	app.logInfo(r.Context(), "视频生成完成", "returned", len(data))
	writeJSON(w, http.StatusOK, map[string]any{"created": time.Now().Unix(), "data": data})
}

func (app *App) createVideoURLs(ctx context.Context, model, promptText string, videoOptions map[string]any) ([]string, error) {
	var lastErr error
	attempts := app.mediaRetryAttempts()
	for attempt := 0; attempt < attempts; attempt++ {
		acc, err := app.accounts.AcquireFor(ctx, "", accountUsageVideo)
		if err != nil {
			app.logWarn(ctx, "视频生成获取账号失败", "attempt", attempt+1, "error", err)
			return nil, err
		}
		chatID := ""
		func() {
			defer app.accounts.Release(acc)
			setRequestLogFields(ctx, "account", acc.Email)
			app.logInfo(ctx, "视频生成开始尝试", "attempt", attempt+1, "model", model)
			chatID, err = app.client.CreateChat(ctx, qwenCreds(acc.Token, acc.Cookies), model, "t2v")
			if err != nil {
				app.classifyAccountErrorFor(acc, err, accountUsageVideo)
				lastErr = err
				app.logWarn(ctx, "视频生成创建会话失败", "attempt", attempt+1, "error", err)
				return
			}
			defer asyncDeleteChat(app.client, acc.Token, acc.Cookies, chatID)
			setRequestLogFields(ctx, "chat_id", chatID)

			payload := buildChatPayload(chatID, model, promptText, false, nil, "t2v", videoOptions, nil, false)
			payload["stream"] = false
			status, body, err := app.client.PostChatCompletionOnce(ctx, qwenCreds(acc.Token, acc.Cookies), chatID, payload, 90*time.Second)
			if err != nil {
				app.classifyAccountErrorFor(acc, err, accountUsageVideo)
				lastErr = err
				app.logWarn(ctx, "视频生成上游请求失败", "attempt", attempt+1, "error", err)
				return
			}
			if status != http.StatusOK {
				lastErr = fmt.Errorf("video completion HTTP %d: %s", status, truncate(body, 500))
				app.classifyAccountErrorFor(acc, lastErr, accountUsageVideo)
				app.logWarn(ctx, "视频生成上游状态异常", "attempt", attempt+1, "status", status, "body", truncate(body, 240))
				return
			}
			answerText := body
			if failure := extractUpstreamFailure(answerText); failure != "" {
				lastErr = fmt.Errorf("%s", failure)
				app.classifyAccountErrorFor(acc, lastErr, accountUsageVideo)
				app.logWarn(ctx, "视频生成上游返回失败", "attempt", attempt+1, "failure", failure)
				return
			}
			urls := extractVideoURLs(answerText)
			taskIDs := extractTaskIDs(answerText)
			app.logInfo(ctx, "视频生成初始结果解析", "attempt", attempt+1, "url_count", len(urls), "task_count", len(taskIDs))
			if len(urls) == 0 && len(taskIDs) > 0 {
				app.logInfo(ctx, "视频生成开始轮询任务", "task_id", taskIDs[0])
				taskText, err := app.pollVideoTask(ctx, qwenCreds(acc.Token, acc.Cookies), taskIDs[0], 7*time.Minute)
				if err != nil {
					lastErr = err
					app.classifyAccountErrorFor(acc, err, accountUsageVideo)
					app.logWarn(ctx, "视频生成任务轮询失败", "task_id", taskIDs[0], "error", err)
					return
				}
				answerText += "\n" + taskText
				urls = extractVideoURLs(answerText)
			}
			if len(urls) == 0 {
				if _, detail, err := app.client.GetChatDetail(ctx, qwenCreds(acc.Token, acc.Cookies), chatID, 30*time.Second); err == nil && detail != "" {
					answerText += "\n" + detail
					urls = extractVideoURLs(answerText)
				}
			}
			if len(urls) == 0 {
				lastErr = fmt.Errorf("Video generation produced no video URL (chat_id=%s)", chatID)
				app.logWarn(ctx, "视频生成未识别到链接", "attempt", attempt+1, "answer_tail", promptTail(answerText, 240))
				return
			}
			app.accounts.MarkSuccessFor(acc, accountUsageVideo)
			lastErr = nil
			videoOptions["urls"] = urls
			app.logInfo(ctx, "视频生成尝试成功", "attempt", attempt+1, "url_count", len(urls))
		}()
		if lastErr == nil {
			if urls, ok := videoOptions["urls"].([]string); ok {
				return urls, nil
			}
		}
		app.logWarn(ctx, "视频生成尝试失败", "attempt", attempt+1, "error", lastErr)
		if !app.accounts.HasAvailableFor(accountUsageVideo) {
			app.logWarn(ctx, "视频生成账号池已无可用账号", "attempt", attempt+1, "error", lastErr)
			break
		}
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("video generation failed")
	}
	return nil, fmt.Errorf("All %d attempts failed. Last error: %w", attempts, lastErr)
}

func (app *App) mediaRetryAttempts() int {
	attempts := max(1, app.settings.MaxRetries)
	if app != nil && app.accounts != nil {
		attempts = max(attempts, len(app.accounts.Snapshot()))
	}
	return attempts
}

func upstreamMediaErrorStatus(err error) int {
	if err == nil {
		return http.StatusInternalServerError
	}
	msg := err.Error()
	if isRateLimitErrorMessage(msg) {
		return http.StatusTooManyRequests
	}
	if isModelNotFoundErrorMessage(msg) {
		return http.StatusBadRequest
	}
	return http.StatusInternalServerError
}

func (app *App) pollVideoTask(ctx context.Context, creds QwenCreds, taskID string, timeout time.Duration) (string, error) {
	deadline := time.Now().Add(timeout)
	snapshots := []string{}
	lastStatus := ""
	for time.Now().Before(deadline) {
		status, body, err := app.client.GetVisionTaskStatus(ctx, creds, taskID, 30*time.Second)
		if body != "" {
			snapshots = append(snapshots, body)
		}
		if err == nil && status == http.StatusOK {
			taskStatus := taskStatusFromBody(body)
			if taskStatus != "" {
				lastStatus = taskStatus
			}
			if mediaStatusSuccess[taskStatus] {
				return strings.Join(snapshots, "\n"), nil
			}
			if taskStatus != "" && !mediaStatusRunning[taskStatus] {
				return "", fmt.Errorf("Video task failed status=%s body=%s", taskStatus, truncate(body, 500))
			}
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(10 * time.Second):
		}
	}
	return "", fmt.Errorf("Video task timed out task_id=%s last_status=%s", taskID, firstNonEmpty(lastStatus, "-"))
}

func normalizeMediaSize(value string) (string, string) {
	v := strings.ToLower(strings.ReplaceAll(strings.ReplaceAll(strings.TrimSpace(value), "*", "x"), "×", "x"))
	sizes := map[string]string{"1328x1328": "1:1", "1664x928": "16:9", "928x1664": "9:16", "1472x1140": "4:3", "1140x1472": "3:4"}
	ratios := map[string]string{"1:1": "1328x1328", "16:9": "1664x928", "9:16": "928x1664", "4:3": "1472x1140", "3:4": "1140x1472"}
	if ratio, ok := sizes[v]; ok {
		return v, ratio
	}
	if size, ok := ratios[v]; ok {
		return size, v
	}
	return "1328x1328", "1:1"
}

func mediaDimensions(size string) (int, int) {
	parts := strings.SplitN(size, "x", 2)
	if len(parts) != 2 {
		return 1328, 1328
	}
	width, errW := strconv.Atoi(parts[0])
	height, errH := strconv.Atoi(parts[1])
	if errW != nil || errH != nil || width <= 0 || height <= 0 {
		return 1328, 1328
	}
	return width, height
}

func resolveMediaModel(requested string, image bool) string {
	requested = strings.TrimSpace(requested)
	if requested == "" {
		return "qwen3.6-plus"
	}
	aliases := map[string]string{
		"dall-e-3": "qwen3.6-plus", "dall-e-2": "qwen3.6-plus", "gpt-image-1": "qwen3.6-plus",
		"qwen-image": "qwen3.6-plus", "qwen-image-plus": "qwen3.6-plus", "qwen-image-turbo": "qwen3.6-plus",
		"qwen-video": "qwen3.6-plus", "qwen-video-plus": "qwen3.6-plus", "qwen-video-turbo": "qwen3.6-plus",
		"sora": "qwen3.6-plus", "sora-2": "qwen3.6-plus",
	}
	if v, ok := aliases[strings.ToLower(requested)]; ok {
		return v
	}
	mode := parseModelMode(requested, "qwen3.6-plus")
	return resolveModel(mode.BaseModel)
}

var (
	imageURLKeys       = map[string]bool{"url": true, "image": true, "src": true, "imageUrl": true, "image_url": true, "imageURL": true, "preview_url": true, "previewUrl": true, "download_url": true, "downloadUrl": true, "origin_url": true, "originUrl": true, "oss_url": true, "ossUrl": true, "signed_url": true, "signedUrl": true}
	videoURLKeys       = map[string]bool{"url": true, "video": true, "src": true, "videoUrl": true, "video_url": true, "videoURL": true, "preview_url": true, "previewUrl": true, "download_url": true, "downloadUrl": true, "origin_url": true, "originUrl": true, "oss_url": true, "ossUrl": true, "signed_url": true, "signedUrl": true}
	taskIDKeys         = map[string]bool{"task_id": true, "taskId": true, "wanx_task_id": true, "wanxTaskId": true}
	mediaStatusRunning = map[string]bool{"running": true, "pending": true, "queued": true, "processing": true, "created": true}
	mediaStatusSuccess = map[string]bool{"success": true, "succeeded": true, "finished": true, "completed": true}
)

func extractImageURLs(text string) []string {
	urls := []string{}
	appendMatches(&urls, text, `!\[.*?\]\((https?://[^\s\)]+)\)`, true)
	appendMatches(&urls, text, `"(?:url|image|src|imageUrl|image_url)"\s*:\s*"(https?://[^"]+)"`, true)
	appendMatches(&urls, text, `https?://(?:cdn\.qwenlm\.ai|wanx\.alicdn\.com|img\.alicdn\.com|[^\s"<>]+\.(?:jpg|jpeg|png|webp|gif))(?:[^\s"<>]*)`, false)
	forEachJSONFragment(text, func(value any) {
		collectMediaURLs(value, &urls, imageURLKeys, looksLikeImageURL)
	})
	return dedupeStrings(urls)
}

func extractVideoURLs(text string) []string {
	urls := []string{}
	appendMatches(&urls, text, `!\[.*?\]\((https?://[^\s\)]+)\)`, true)
	appendMatches(&urls, text, `"(?:url|video|src|videoUrl|video_url)"\s*:\s*"(https?://[^"]+)"`, true)
	appendMatches(&urls, text, `https?://[^\s"<>]+\.(?:mp4|webm|mov|m3u8)(?:[^\s"<>]*)`, false)
	forEachJSONFragment(text, func(value any) {
		collectMediaURLs(value, &urls, videoURLKeys, looksLikeVideoURL)
	})
	filtered := []string{}
	for _, url := range urls {
		if looksLikeVideoURL(url) {
			filtered = append(filtered, url)
		}
	}
	return dedupeStrings(filtered)
}

func extractTaskIDs(text string) []string {
	taskIDs := []string{}
	forEachJSONFragment(text, func(value any) {
		collectTaskIDs(value, &taskIDs)
	})
	return dedupeStrings(taskIDs)
}

func extractUpstreamFailure(text string) string {
	out := ""
	forEachJSONFragment(text, func(value any) {
		if out != "" {
			return
		}
		obj, ok := value.(map[string]any)
		if !ok {
			return
		}
		requestID := firstNonEmpty(firstString(obj["request_id"], obj["response_id"]), "-")
		if b, ok := obj["success"].(bool); ok && !b {
			data, _ := obj["data"].(map[string]any)
			code := firstNonEmpty(firstString(data["code"], obj["code"]), "upstream_error")
			details := firstNonEmpty(firstString(data["details"], data["message"], obj["details"], obj["message"]), "")
			out = fmt.Sprintf("Qwen upstream error code=%s request_id=%s details=%s", code, requestID, details)
			return
		}
		switch errValue := obj["error"].(type) {
		case map[string]any:
			code := firstNonEmpty(firstString(errValue["code"]), "upstream_error")
			details := firstNonEmpty(firstString(errValue["details"], errValue["message"], errValue["type"]), "")
			out = fmt.Sprintf("Qwen upstream error code=%s request_id=%s details=%s", code, requestID, details)
		case string:
			if errValue != "" {
				out = fmt.Sprintf("Qwen upstream error request_id=%s details=%s", requestID, errValue)
			}
		}
	})
	return out
}

func taskStatusFromBody(body string) string {
	status := ""
	forEachJSONFragment(body, func(value any) {
		if status != "" {
			return
		}
		obj, ok := value.(map[string]any)
		if !ok {
			return
		}
		data, _ := obj["data"].(map[string]any)
		status = normalizeLower(firstString(obj["task_status"], obj["status"], data["task_status"], data["status"]))
	})
	return status
}

func appendMatches(out *[]string, text, pattern string, submatch bool) {
	re := regexp.MustCompile(`(?i)` + pattern)
	for _, match := range re.FindAllStringSubmatch(text, -1) {
		value := ""
		if submatch && len(match) > 1 {
			value = match[1]
		} else if len(match) > 0 {
			value = match[0]
		}
		value = strings.TrimRight(value, ".,;)\"'>")
		if value != "" {
			*out = append(*out, value)
		}
	}
}

func forEachJSONFragment(text string, visit func(any)) {
	for idx, r := range text {
		if r != '{' && r != '[' {
			continue
		}
		dec := json.NewDecoder(strings.NewReader(text[idx:]))
		var value any
		if err := dec.Decode(&value); err == nil {
			visit(value)
		}
	}
}

func collectMediaURLs(value any, urls *[]string, keys map[string]bool, looks func(string) bool) {
	switch v := value.(type) {
	case map[string]any:
		for key, item := range v {
			if s, ok := item.(string); ok && (keys[key] || looks(s)) && looks(s) {
				*urls = append(*urls, s)
				continue
			}
			collectMediaURLs(item, urls, keys, looks)
		}
	case []any:
		for _, item := range v {
			collectMediaURLs(item, urls, keys, looks)
		}
	}
}

func collectTaskIDs(value any, taskIDs *[]string) {
	switch v := value.(type) {
	case map[string]any:
		for key, item := range v {
			if taskIDKeys[key] {
				if s, ok := item.(string); ok && s != "" {
					*taskIDs = append(*taskIDs, s)
					continue
				}
			}
			collectTaskIDs(item, taskIDs)
		}
	case []any:
		for _, item := range v {
			collectTaskIDs(item, taskIDs)
		}
	}
}

func looksLikeImageURL(value string) bool {
	if !strings.HasPrefix(value, "http://") && !strings.HasPrefix(value, "https://") {
		return false
	}
	lowered := strings.ToLower(value)
	for _, host := range []string{"cdn.qwenlm.ai", "wanx.alicdn.com", "img.alicdn.com", "alicdn.com"} {
		if strings.Contains(lowered, host) {
			return true
		}
	}
	return regexp.MustCompile(`\.(?:jpg|jpeg|png|webp|gif)(?:[?#][^\s"'<>]*)?$`).MatchString(lowered)
}

func looksLikeVideoURL(value string) bool {
	if !strings.HasPrefix(value, "http://") && !strings.HasPrefix(value, "https://") {
		return false
	}
	lowered := strings.ToLower(value)
	if regexp.MustCompile(`\.(?:mp4|webm|mov|m3u8)(?:[?#][^\s"'<>]*)?$`).MatchString(lowered) {
		return true
	}
	return (strings.Contains(lowered, "cdn.qwenlm.ai") || strings.Contains(lowered, "wanx.alicdn.com") || strings.Contains(lowered, "alicdn.com")) &&
		(strings.Contains(lowered, "video") || strings.Contains(lowered, "mp4") || strings.Contains(lowered, "t2v"))
}

func dedupeStrings(values []string) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, value := range values {
		value = strings.TrimRight(strings.TrimSpace(value), ".,;)\"'>")
		if value == "" || seen[value] {
			continue
		}
		seen[value] = true
		out = append(out, value)
	}
	return out
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

func mustJSON(v any) string {
	raw, _ := json.Marshal(v)
	return string(raw)
}

func (app *App) adminStatus(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	perAccount := []map[string]any{}
	for _, acc := range app.accounts.Snapshot() {
		perAccount = append(perAccount, map[string]any{
			"email": acc.Email, "status": acc.StatusCode, "inflight": acc.Inflight,
			"max_inflight": app.settings.MaxInflightPerAccount, "consecutive_failures": acc.ConsecutiveFailures,
			"rate_limit_strikes": acc.RateLimitStrikes, "last_request_finished": acc.LastRequestFinished,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"accounts":           app.accounts.Status(),
		"per_account":        perAccount,
		"chat_id_pool":       app.chatPool.Status(),
		"runtime":            map[string]any{"mode": "go", "goroutines_note": "not exposed"},
		"request_runtime":    map[string]any{"mode": "direct_http", "browser_required_for_requests": false, "description": "普通请求直连 HTTP，不经过浏览器"},
		"browser_automation": map[string]any{"mode": "playwright", "description": "Go 后端通过 Playwright 浏览器自动化支持邮箱激活"},
	})
}

func (app *App) adminListUsers(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	var users []map[string]any
	_ = app.usersStore.LoadInto(&users)
	writeJSON(w, http.StatusOK, map[string]any{"users": users})
}

func (app *App) adminCreateUser(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	var body map[string]any
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	var users []map[string]any
	_ = app.usersStore.LoadInto(&users)
	user := map[string]any{"id": "sk-" + randomID(), "name": stringValue(body, "name", ""), "quota": intValue(body, "quota", 1000000), "used_tokens": 0}
	users = append(users, user)
	_ = app.usersStore.Save(users)
	writeJSON(w, http.StatusOK, user)
}

func (app *App) adminListAccounts(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	accounts := []map[string]any{}
	for _, acc := range app.accounts.Snapshot() {
		accounts = append(accounts, map[string]any{
			"email": acc.Email, "password": acc.Password, "token": acc.Token, "cookies": acc.Cookies,
			"username": acc.Username, "activation_pending": acc.ActivationPending, "status_code": acc.StatusCode,
			"source": acc.Source, "env_name": acc.EnvName,
			"last_error": acc.LastError, "last_request_started": acc.LastRequestStarted, "last_request_finished": acc.LastRequestFinished,
			"consecutive_failures": acc.ConsecutiveFailures, "rate_limit_strikes": acc.RateLimitStrikes,
			"valid": acc.Valid, "inflight": acc.Inflight, "rate_limited_until": acc.RateLimitedUntil,
			"rate_limits": cloneRateLimits(acc.RateLimits),
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"accounts": accounts})
}

func (app *App) adminAddAccount(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	var body map[string]any
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	token := stringValue(body, "token", "")
	if token == "" {
		writeError(w, http.StatusBadRequest, "token is required")
		return
	}
	acc := Account{
		Email:      stringValue(body, "email", fmt.Sprintf("manual_%d@qwen", time.Now().Unix())),
		Password:   stringValue(body, "password", ""),
		Token:      token,
		Cookies:    stringValue(body, "cookies", ""),
		Username:   stringValue(body, "username", ""),
		StatusCode: "valid",
	}
	verify := app.client.VerifyTokenDetail(r.Context(), qwenCreds(token, stringValue(body, "cookies", "")))
	if !verify.Valid {
		writeJSON(w, http.StatusOK, map[string]any{"ok": false, "error": "Invalid token (验证失败，请确认Token有效)", "status_code": verify.StatusCode, "detail": verify.Error})
		return
	}
	if err := app.accounts.Add(acc); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "email": acc.Email})
}

func (app *App) adminVerifyAll(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	results := []map[string]any{}
	for _, acc := range app.accounts.Snapshot() {
		verify := app.client.VerifyTokenDetail(r.Context(), qwenCreds(acc.Token, acc.Cookies))
		_ = app.accounts.MarkVerification(acc.Email, verify)
		results = append(results, map[string]any{"email": acc.Email, "valid": verify.Valid, "status_code": verify.StatusCode, "error": verify.Error, "refreshed": false})
	}
	validCount := 0
	bannedCount := 0
	for _, result := range results {
		if result["valid"] == true {
			validCount++
		}
		if result["status_code"] == "banned" {
			bannedCount++
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "results": results, "summary": map[string]any{"total": len(results), "valid": validCount, "refreshed": 0, "banned": bannedCount, "failed": len(results) - validCount}, "concurrency": 1})
}

func (app *App) adminActivateAccount(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	email := r.PathValue("email")
	app.logInfo(r.Context(), "账号激活请求进入", "account", email)
	var target *Account
	for _, acc := range app.accounts.Snapshot() {
		if acc.Email == email {
			cp := acc
			target = &cp
			break
		}
	}
	if target == nil {
		app.logWarn(r.Context(), "账号激活目标不存在", "account", email)
		writeError(w, http.StatusNotFound, "Account not found")
		return
	}
	if target.Valid && target.Token != "" && !target.ActivationPending {
		verify := app.client.VerifyTokenDetail(r.Context(), qwenCreds(target.Token, target.Cookies))
		if verify.Valid && strings.TrimSpace(target.Cookies) != "" {
			_ = app.accounts.MarkVerification(target.Email, verify)
			app.logInfo(r.Context(), "账号已激活，现有 token 验证通过", "account", email)
			writeJSON(w, http.StatusOK, map[string]any{"ok": true, "message": "账号已激活，现有 token 验证通过"})
			return
		}
		if verify.Valid && strings.TrimSpace(target.Cookies) == "" && target.Password != "" {
			ctx, cancel := context.WithTimeout(r.Context(), 5*time.Minute)
			defer cancel()
			refreshed, err := app.refreshAccountSession(ctx, *target)
			if err == nil {
				if err := app.accounts.Add(refreshed); err != nil {
					app.logWarn(r.Context(), "账号 Cookie 刷新保存失败", "account", email, "error", err)
					writeError(w, http.StatusInternalServerError, err.Error())
					return
				}
				app.logInfo(r.Context(), "账号 Cookie 刷新成功", "account", email)
				writeJSON(w, http.StatusOK, map[string]any{"ok": true, "message": "账号 Cookie 刷新成功"})
				return
			}
			app.logWarn(r.Context(), "账号 Cookie 刷新失败，继续激活流程", "account", email, "error", err)
		} else if verify.Valid {
			_ = app.accounts.MarkVerification(target.Email, verify)
			app.logInfo(r.Context(), "账号已激活，现有 token 验证通过", "account", email)
			writeJSON(w, http.StatusOK, map[string]any{"ok": true, "message": "账号已激活，现有 token 验证通过（Cookie 为空，建议补充密码后重新激活）"})
			return
		}
		app.logWarn(r.Context(), "账号标记有效但现有 token 验证失败，继续激活流程", "account", email, "status_code", verify.StatusCode, "error", verify.Error)
	}
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Minute)
	defer cancel()
	updated, ok, err := app.activateQwenAccount(ctx, *target)
	if err != nil || !ok {
		msg := "未能找到激活链接或获取Token"
		if err != nil {
			msg = err.Error()
		}
		app.logWarn(r.Context(), "账号激活失败", "account", email, "error", msg)
		writeJSON(w, http.StatusOK, map[string]any{"ok": false, "error": msg})
		return
	}
	if err := app.accounts.Add(updated); err != nil {
		app.logWarn(r.Context(), "账号激活保存失败", "account", email, "error", err)
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	app.logInfo(r.Context(), "账号激活成功", "account", email)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "message": "账号激活成功"})
}

func (app *App) adminVerifyAccount(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	email := r.PathValue("email")
	for _, acc := range app.accounts.Snapshot() {
		if acc.Email == email {
			verify := app.client.VerifyTokenDetail(r.Context(), qwenCreds(acc.Token, acc.Cookies))
			_ = app.accounts.MarkVerification(acc.Email, verify)
			writeJSON(w, http.StatusOK, map[string]any{"email": email, "valid": verify.Valid, "status_code": verify.StatusCode, "error": verify.Error, "refreshed": false})
			return
		}
	}
	writeError(w, http.StatusNotFound, "Account not found")
}

func (app *App) adminDeleteAccount(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	if err := app.accounts.Remove(r.PathValue("email")); err != nil {
		if strings.Contains(err.Error(), "environment account") {
			writeError(w, http.StatusBadRequest, "环境变量注入账号不能在面板删除，请移除对应环境变量后重启服务")
			return
		}
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (app *App) adminGetSettings(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	keepaliveCfg := app.keepaliveConfig()
	keepaliveStatus := map[string]any{"running": false}
	if app.keepalive != nil {
		keepaliveStatus = app.keepalive.Status()
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"version":                      Version,
		"max_inflight_per_account":     app.settings.MaxInflightPerAccount,
		"global_max_inflight":          app.accounts.Status()["global_max_inflight"],
		"max_queue_size":               app.accounts.Status()["max_queue_size"],
		"account_ready_set_threshold":  app.settings.AccountReadySetThreshold,
		"account_ready_set_enabled":    app.accounts.Status()["ready_set_enabled"],
		"chat_id_pool_target":          app.settings.ChatIDPrewarmTargetPerAccount,
		"chat_id_pool_ttl_seconds":     app.settings.ChatIDPrewarmTTLSeconds,
		"chat_id_pool_max_concurrency": app.settings.ChatIDPrewarmMaxConcurrency,
		"keepalive_url":                keepaliveCfg.URL,
		"keepalive_interval":           keepaliveCfg.Interval,
		"keepalive_env_locked":         keepaliveCfg.EnvLocked,
		"keepalive_running":            boolValue(keepaliveStatus["running"]),
		"keepalive_status":             keepaliveStatus,
		"model_aliases":                modelMap,
	})
}

func (app *App) adminUpdateSettings(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	var body map[string]any
	if err := decodeJSON(r, &body); err != nil {
		writeError(w, http.StatusBadRequest, "Invalid JSON body")
		return
	}
	if _, ok := body["max_inflight_per_account"]; ok {
		app.settings.MaxInflightPerAccount = intValue(body, "max_inflight_per_account", app.settings.MaxInflightPerAccount)
		app.accounts.SetMaxInflight(app.settings.MaxInflightPerAccount)
	}
	if _, ok := body["account_ready_set_threshold"]; ok {
		app.settings.AccountReadySetThreshold = max(1, intValue(body, "account_ready_set_threshold", app.settings.AccountReadySetThreshold))
		app.accounts.SetReadySetThreshold(app.settings.AccountReadySetThreshold)
	}
	if _, ok := body["global_max_inflight"]; ok {
		app.accounts.SetGlobalMaxInflight(intValue(body, "global_max_inflight", 0))
	}
	if _, ok := body["chat_id_pool_target"]; ok {
		app.settings.ChatIDPrewarmTargetPerAccount = max(0, intValue(body, "chat_id_pool_target", app.settings.ChatIDPrewarmTargetPerAccount))
	}
	if _, ok := body["chat_id_pool_ttl_seconds"]; ok {
		app.settings.ChatIDPrewarmTTLSeconds = max(1, intValue(body, "chat_id_pool_ttl_seconds", app.settings.ChatIDPrewarmTTLSeconds))
	}
	if _, ok := body["chat_id_pool_max_concurrency"]; ok {
		app.settings.ChatIDPrewarmMaxConcurrency = max(1, intValue(body, "chat_id_pool_max_concurrency", app.settings.ChatIDPrewarmMaxConcurrency))
	}
	if err := app.updateKeepAliveSettings(body); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if app.chatPool != nil {
		app.chatPool.UpdateSettings(app.settings)
		if app.settings.ChatIDPrewarmTargetPerAccount > 0 {
			go app.chatPool.Fill(context.Background())
		}
	}
	if aliases, ok := body["model_aliases"].(map[string]any); ok {
		next := map[string]string{}
		for k, v := range aliases {
			if s, ok := v.(string); ok {
				next[k] = s
			}
		}
		if len(next) > 0 {
			modelMap = next
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (app *App) adminGetKeys(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	keys := []string{}
	items := []map[string]any{}
	for key := range app.apiKeys {
		keys = append(keys, key)
		source := "managed"
		label := "面板创建 Key"
		if app.envAPIKeys[key] {
			source = "env"
			label = "环境变量注入 Key"
		}
		items = append(items, map[string]any{"key": key, "source": source, "label": label})
	}
	sort.Strings(keys)
	sort.Slice(items, func(i, j int) bool {
		return fmt.Sprint(items[i]["key"]) < fmt.Sprint(items[j]["key"])
	})
	writeJSON(w, http.StatusOK, map[string]any{"keys": keys, "items": items})
}

func (app *App) adminCreateKey(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	var body struct {
		Mode string `json:"mode"`
		Key  string `json:"key"`
	}
	raw, _ := io.ReadAll(r.Body)
	if len(strings.TrimSpace(string(raw))) > 0 {
		if err := json.Unmarshal(raw, &body); err != nil {
			writeError(w, http.StatusBadRequest, "Invalid JSON body")
			return
		}
	}
	mode := normalizeLower(body.Mode)
	if mode == "" {
		mode = "auto"
	}
	key := strings.TrimSpace(body.Key)
	if mode == "custom" {
		if key == "" {
			writeError(w, http.StatusBadRequest, "自定义 Key 不能为空")
			return
		}
		if strings.ContainsAny(key, " \t\r\n") {
			writeError(w, http.StatusBadRequest, "自定义 Key 不能包含空白字符")
			return
		}
	} else {
		buf := make([]byte, 24)
		_, _ = cryptorand.Read(buf)
		key = "sk-" + hex.EncodeToString(buf)
	}
	if app.apiKeys[key] {
		writeError(w, http.StatusConflict, "API Key 已存在")
		return
	}
	app.apiKeys[key] = true
	app.managedAPIKeys[key] = true
	_ = saveAPIKeys(app.settings.APIKeysFile, app.managedAPIKeys)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "key": key})
}

func (app *App) adminDeleteKey(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	key := r.PathValue("key")
	if app.envAPIKeys[key] {
		writeError(w, http.StatusBadRequest, "环境变量注入 Key 不能在面板删除，请移除对应环境变量后重启服务")
		return
	}
	delete(app.apiKeys, key)
	delete(app.managedAPIKeys, key)
	_ = saveAPIKeys(app.settings.APIKeysFile, app.managedAPIKeys)
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (app *App) adminGetCaptures(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	data, err := app.capturesStore.LoadAny()
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, data)
}

func (app *App) adminDeleteCaptures(w http.ResponseWriter, r *http.Request) {
	if _, ok := app.verifyAdmin(w, r); !ok {
		return
	}
	_ = app.capturesStore.Save([]any{})
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

// ---- migrated from models.go ----
func buildModelEntry(modelID, baseModel string, capabilities map[string]bool, mode, displayName, family string, created int64, ownedBy string) map[string]any {
	if baseModel == "" {
		baseModel = modelID
	}
	if displayName == "" {
		displayName = modelID
	}
	if family == "" {
		family = baseModel
	}
	if created == 0 {
		created = openAIModelCreatedEpoch
	}
	if ownedBy == "" {
		ownedBy = "qwen"
	}
	return map[string]any{
		"id":           modelID,
		"object":       "model",
		"created":      created,
		"owned_by":     ownedBy,
		"capabilities": capabilities,
		"base_model":   baseModel,
		"mode":         mode,
		"display_name": displayName,
		"family":       family,
	}
}

func buildFallbackModelList() map[string]any {
	return services.BuildFallbackModelList(modelMap)
}

func buildOpenAIModelList(upstream []map[string]any) map[string]any {
	return services.BuildOpenAIModelList(upstream)
}

func extractModelCapabilities(item map[string]any) map[string]bool {
	caps := map[string]bool{
		"thinking":      false,
		"search":        false,
		"vision":        false,
		"deep_research": false,
		"image_gen":     false,
		"video_gen":     false,
		"web_dev":       false,
		"slides":        false,
	}
	meta := map[string]any{}
	if info, ok := item["info"].(map[string]any); ok {
		if m, ok := info["meta"].(map[string]any); ok {
			meta = m
		}
	}
	if m, ok := item["meta"].(map[string]any); ok {
		for k, v := range m {
			meta[k] = v
		}
	}
	if rawCaps, ok := meta["capabilities"].(map[string]any); ok {
		for k, v := range rawCaps {
			if b, ok := v.(bool); ok {
				caps[k] = b
			}
		}
	}
	applyChatType := func(chatType string) {
		switch chatType {
		case "deep_research":
			caps["deep_research"] = true
		case "t2i", "image_gen":
			caps["image_gen"] = true
		case "t2v":
			caps["video_gen"] = true
		case "web_dev":
			caps["web_dev"] = true
		case "slides":
			caps["slides"] = true
		}
	}
	switch v := meta["chat_type"].(type) {
	case string:
		applyChatType(v)
	case []any:
		for _, item := range v {
			applyChatType(anyString(item, ""))
		}
	}
	return caps
}

func deriveFamily(modelID string, item map[string]any) string {
	if family := anyString(item["family"], ""); family != "" {
		return family
	}
	if strings.HasPrefix(modelID, "qwen3.") {
		parts := strings.Split(strings.SplitN(modelID, "-", 2)[0], ".")
		if len(parts) >= 2 {
			return parts[0] + "." + parts[1]
		}
	}
	if idx := strings.Index(modelID, "-"); idx > 0 {
		return modelID[:idx]
	}
	return modelID
}

// ---- migrated from protocol.go ----
type StandardRequest struct {
	Prompt          string
	ResponseModel   string
	ResolvedModel   string
	Surface         string
	Stream          bool
	Tools           []map[string]any
	ToolNames       []string
	ToolEnabled     bool
	ChatType        string
	ThinkingEnabled *bool
	ForceThinking   bool
	EnableSearch    bool
	ModelMode       string
	SessionKey      string
	WorkspaceRoot   string
	ClientProfile   string
	ContextMode     string
	UpstreamFiles   []map[string]any
	PreferredEmail  string
	BoundAccount    *Account

	RepeatedToolName      string
	RepeatedToolSignature string
	RepeatedToolCount     int
}

type CompletionResult struct {
	AnswerText                string
	ReasoningText             string
	Events                    []UpstreamEvent
	ToolCalls                 []ParsedToolCall
	FinishReason              string
	InvalidToolCallSignatures []string
	InvalidToolCallReasons    []string
}

type completionStreamHooks struct {
	OnReasoningDelta func(string) error
	OnAnswerDelta    func(string) error
	OnToolCalls      func([]ParsedToolCall) error
}

var errToolSieveDetected = errors.New("tool_sieve_detected")

func buildChatStandardRequest(body map[string]any, defaultModel, surface string) StandardRequest {
	req := adapter.BuildChatStandardRequest(
		body,
		defaultModel,
		surface,
		resolveModel,
		func(modelID, defaultModel string) adapter.ModelMode {
			mode := parseModelMode(modelID, defaultModel)
			return adapter.ModelMode{
				RequestedModel: mode.RequestedModel,
				BaseModel:      mode.BaseModel,
				ChatType:       mode.ChatType,
				ForceThinking:  mode.ForceThinking,
				Mode:           mode.Mode,
			}
		},
	)
	mode := parseModelMode(req.ResponseModel, defaultModel)
	thinking := req.ThinkingEnabled
	if mode.ForceThinking {
		v := true
		thinking = &v
	}
	return StandardRequest{
		Prompt:                req.Prompt,
		ResponseModel:         mode.RequestedModel,
		ResolvedModel:         resolveModel(mode.BaseModel),
		Surface:               req.Surface,
		Stream:                req.Stream,
		Tools:                 req.Tools,
		ToolNames:             req.ToolNames,
		ToolEnabled:           req.ToolEnabled,
		ChatType:              mode.ChatType,
		ThinkingEnabled:       thinking,
		ForceThinking:         mode.ForceThinking,
		EnableSearch:          req.EnableSearch,
		ModelMode:             mode.Mode,
		RepeatedToolName:      req.RepeatedToolName,
		RepeatedToolSignature: req.RepeatedToolSignature,
		RepeatedToolCount:     req.RepeatedToolCount,
	}
}

func extractThinkingEnabled(body map[string]any) *bool {
	if value, ok := body["enable_thinking"]; ok {
		return coerceBool(value)
	}
	if value, ok := body["thinking"]; ok {
		if m, ok := value.(map[string]any); ok {
			for _, key := range []string{"enabled", "enable", "enabled_thinking", "enable_thinking"} {
				if inner, ok := m[key]; ok {
					return coerceBool(inner)
				}
			}
		}
		return coerceBool(value)
	}
	if value, ok := body["thinking_mode"]; ok {
		return coerceBool(value)
	}
	return nil
}

func messagesToPrompt(body map[string]any) (string, []map[string]any) {
	var parts []string
	for _, raw := range anyList(body["messages"]) {
		msg, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		role := stringValue(msg, "role", "user")
		text := extractContentText(msg["content"])
		if text == "" {
			continue
		}
		switch role {
		case "system", "developer":
			parts = append(parts, "[System]\n"+text)
		case "assistant":
			parts = append(parts, "[Assistant]\n"+text)
		case "tool":
			parts = append(parts, "[Tool Result]\n"+text)
		default:
			parts = append(parts, "[User]\n"+text)
		}
	}
	tools := normalizeTools(body["tools"])
	if len(tools) > 0 {
		parts = append(parts, buildToolInstructions(tools))
	}
	return strings.Join(parts, "\n\n"), tools
}

func extractContentText(content any) string {
	switch v := content.(type) {
	case nil:
		return ""
	case string:
		return compactSystemReminders(v)
	case []any:
		var parts []string
		for _, item := range v {
			m, ok := item.(map[string]any)
			if !ok {
				continue
			}
			switch stringValue(m, "type", "") {
			case "text", "input_text":
				if text := compactSystemReminders(stringValue(m, "text", "")); text != "" {
					parts = append(parts, text)
				}
			case "image_url", "input_image", "input_file":
				raw, _ := json.Marshal(m)
				parts = append(parts, string(raw))
			case "tool_use":
				raw, _ := json.Marshal(m["input"])
				parts = append(parts, fmt.Sprintf("<tool_use name=%q>%s</tool_use>", stringValue(m, "name", ""), raw))
			case "tool_result":
				raw, _ := json.Marshal(m["content"])
				parts = append(parts, "[Tool Result]\n"+string(raw))
			}
		}
		return strings.Join(parts, "\n")
	default:
		raw, _ := json.Marshal(v)
		return string(raw)
	}
}

func normalizeTools(value any) []map[string]any {
	tools := []map[string]any{}
	for _, raw := range anyList(value) {
		m, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if stringValue(m, "type", "") == "function" {
			if fn, ok := m["function"].(map[string]any); ok {
				tools = append(tools, fn)
				continue
			}
		}
		if stringValue(m, "name", "") != "" {
			tools = append(tools, m)
		}
	}
	return tools
}

func buildToolInstructions(tools []map[string]any) string {
	blocks := []string{
		"IMPORTANT: Reply in the same language as the user. User inputs Chinese -> respond in Chinese.",
		"Use tools only when they are necessary to directly answer the CURRENT TASK.",
	}
	for _, tool := range tools {
		params := tool["parameters"]
		if params == nil {
			params = tool["input_schema"]
		}
		raw, _ := json.Marshal(params)
		blocks = append(blocks, fmt.Sprintf("Tool: %s\nDescription: %s\nParameters: %s", stringValue(tool, "name", ""), trim(stringValue(tool, "description", ""), 100), raw))
	}
	return strings.Join(blocks, "\n\n")
}

func compactSystemReminders(text string) string {
	re := regexp.MustCompile(`(?is)<system-reminder>(.*?)</system-reminder>`)
	return re.ReplaceAllStringFunc(text, func(match string) string {
		sub := re.FindStringSubmatch(match)
		if len(sub) < 2 {
			return "[system-reminder]"
		}
		first := strings.TrimSpace(strings.SplitN(sub[1], "\n", 2)[0])
		if first == "" {
			return "[system-reminder]"
		}
		return "[system-reminder: " + trim(first, 80) + "...]"
	})
}

func buildOpenAICompletionPayload(id string, created int64, req StandardRequest, result CompletionResult) map[string]any {
	content := result.AnswerText
	if content == "" && result.ReasoningText == "" {
		content = emptyCompletionFallback(req, result)
	}
	msg := map[string]any{"role": "assistant", "content": content}
	finishReason := "stop"
	if calls := completionToolCalls(result, req.Tools); len(calls) > 0 {
		msg["content"] = nil
		msg["tool_calls"] = openAIToolCalls(calls)
		finishReason = "tool_calls"
	}
	if result.ReasoningText != "" {
		msg["reasoning_content"] = result.ReasoningText
	}
	return map[string]any{
		"id": id, "object": "chat.completion", "created": created, "model": req.ResponseModel,
		"choices": []map[string]any{{"index": 0, "message": msg, "finish_reason": finishReason}},
		"usage": map[string]any{
			"prompt_tokens": len(req.Prompt), "completion_tokens": len(result.AnswerText),
			"total_tokens": len(req.Prompt) + len(result.AnswerText),
		},
	}
}

func openAIChunk(id string, created int64, model string, delta map[string]any, finish any) string {
	payload := map[string]any{
		"id": id, "object": "chat.completion.chunk", "created": created, "model": model,
		"choices": []map[string]any{{"index": 0, "delta": delta, "finish_reason": finish}},
	}
	raw, _ := json.Marshal(payload)
	return "data: " + string(raw) + "\n\n"
}

func writeSSEEvent(w io.Writer, event string, payload any) {
	_, _ = w.Write([]byte("event: " + event + "\ndata: " + mustJSON(payload) + "\n\n"))
}

func flushSSE(w http.ResponseWriter) {
	if flusher, ok := w.(http.Flusher); ok {
		flusher.Flush()
	}
}

func writeAnthropicToolUseEvents(w io.Writer, calls []ParsedToolCall, startIndex int) {
	for offset, call := range calls {
		index := startIndex + offset
		writeSSEEvent(w, "content_block_start", map[string]any{
			"type":  "content_block_start",
			"index": index,
			"content_block": map[string]any{
				"type":  "tool_use",
				"id":    call.ID,
				"name":  call.Name,
				"input": map[string]any{},
			},
		})
		writeSSEEvent(w, "content_block_delta", map[string]any{
			"type":  "content_block_delta",
			"index": index,
			"delta": map[string]any{"type": "input_json_delta", "partial_json": mustJSON(firstNonNil(call.Input, map[string]any{}))},
		})
		writeSSEEvent(w, "content_block_stop", map[string]any{"type": "content_block_stop", "index": index})
	}
}

func (app *App) buildAnthropicPayload(ctx context.Context, id, model, prompt string, req StandardRequest, result CompletionResult, stage string) map[string]any {
	content, stopReason, outputTokens := app.anthropicContentBlocks(ctx, req, result, stage)
	return map[string]any{
		"id": id, "type": "message", "role": "assistant", "model": model, "content": content,
		"stop_reason": stopReason, "stop_sequence": nil,
		"usage": map[string]any{"input_tokens": len(prompt), "output_tokens": outputTokens},
	}
}

func (app *App) anthropicContentBlocks(ctx context.Context, req StandardRequest, result CompletionResult, stage string) ([]map[string]any, string, int) {
	if calls := completionToolCalls(result, req.Tools); len(calls) > 0 {
		app.logParsedToolCalls(ctx, "ToolCall", stage, calls)
		app.logInfo(ctx, "[ToolDirective]", "tool_blocks", len(calls), "raw_tool_blocks", len(calls), "stop_reason", "tool_use", "has_tool_use", true)
		app.logParsedToolCalls(ctx, "ANT-ToolOut", stage, calls)
		blocks := make([]map[string]any, 0, len(calls))
		for _, call := range calls {
			blocks = append(blocks, map[string]any{
				"type":  "tool_use",
				"id":    call.ID,
				"name":  call.Name,
				"input": firstNonNil(call.Input, map[string]any{}),
			})
		}
		return blocks, "tool_use", 0
	}
	blocks := []map[string]any{}
	if result.ReasoningText != "" {
		blocks = append(blocks, map[string]any{"type": "thinking", "thinking": result.ReasoningText})
	}
	if result.AnswerText != "" {
		blocks = append(blocks, map[string]any{"type": "text", "text": result.AnswerText})
	}
	if len(blocks) == 0 {
		blocks = append(blocks, map[string]any{"type": "text", "text": emptyCompletionFallback(req, result)})
	}
	app.logInfo(ctx, "[ToolDirective]", "tool_blocks", len(blocks), "raw_tool_blocks", len(blocks), "stop_reason", "end_turn", "has_tool_use", false)
	return blocks, "end_turn", len(result.AnswerText)
}

func emptyCompletionFallback(req StandardRequest, result CompletionResult) string {
	switch {
	case result.FinishReason == "invalid_tool_args" || isInvalidToolArgsResult(result):
		return invalidToolMarkupFallback(req, result)
	case result.FinishReason == "missing_tool_continuation":
		return missingToolContinuationFallback()
	case result.FinishReason == "empty":
		return "Upstream returned an empty response. Continue from the last confirmed task state and issue the next required action."
	case strings.HasPrefix(result.FinishReason, "blocked_tool_name:"):
		tool := strings.TrimPrefix(result.FinishReason, "blocked_tool_name:")
		return "Upstream produced invalid tool-availability text for " + firstNonEmpty(tool, "the requested tool") + " and no recoverable QNML tool call. Continue from the last confirmed task state with one fresh complete QNML tool call."
	default:
		return "No recoverable assistant content was produced. Continue from the last confirmed task state."
	}
}

func missingToolContinuationFallback() string {
	return "Upstream stopped after a tool result with narration only and no executable next action. Continue from the last confirmed tool result with one fresh complete QNML tool call."
}

func repeatedToolCallFallback(req StandardRequest, result CompletionResult) string {
	tool := repeatedToolCallName(req, result)
	if tool == "" {
		tool = "the previous tool"
	}
	return "A repeated " + tool + " tool call with the same arguments was blocked to prevent a loop. Continue from the latest confirmed tool result with a different action or different arguments, or state honestly that no further verified progress is possible."
}

func toolParseText(result CompletionResult) string {
	answer := strings.TrimSpace(result.AnswerText)
	reasoning := strings.TrimSpace(result.ReasoningText)
	switch {
	case answer == "":
		return reasoning
	case reasoning == "":
		return answer
	default:
		return answer + "\n" + reasoning
	}
}

func (app *App) logParsedToolCalls(ctx context.Context, label, stage string, calls []ParsedToolCall) {
	for i, call := range calls {
		app.logInfo(ctx, "["+label+"]",
			"stage", stage,
			"index", i+1,
			"id", call.ID,
			"name", call.Name,
			"input", toolInputPreview(call.Input, 320),
		)
	}
}

func toolInputPreview(input any, limit int) string {
	raw := mustJSON(firstNonNil(input, map[string]any{}))
	return truncate(strings.Join(strings.Fields(raw), " "), limit)
}

func parsedToolNames(calls []ParsedToolCall) string {
	names := make([]string, 0, len(calls))
	for _, call := range calls {
		if strings.TrimSpace(call.Name) != "" {
			names = append(names, call.Name)
		}
	}
	return strings.Join(names, ",")
}

func parsedToolCallSignatures(calls []ParsedToolCall) []string {
	signatures := make([]string, 0, len(calls))
	seen := map[string]bool{}
	for _, call := range calls {
		signature := parsedToolCallSignature(call)
		if signature == "" || seen[signature] {
			continue
		}
		seen[signature] = true
		signatures = append(signatures, signature)
	}
	return signatures
}

func collectFinalizeReason(result CompletionResult, calls []ParsedToolCall) string {
	if len(calls) > 0 {
		if result.FinishReason != "" && result.FinishReason != "tool_calls" {
			return result.FinishReason
		}
		return "tool_sieve_detected"
	}
	if result.FinishReason != "" && result.FinishReason != "stop" {
		return result.FinishReason
	}
	if result.AnswerText == "" && result.ReasoningText == "" {
		return "empty"
	}
	return "end_turn"
}

func completionToolCalls(result CompletionResult, tools []map[string]any) []ParsedToolCall {
	if len(result.ToolCalls) > 0 {
		return result.ToolCalls
	}
	return parseToolCalls(toolParseText(result), tools)
}

func hasTextualToolMarker(text string) bool {
	if strings.TrimSpace(text) == "" {
		return false
	}
	lowered := strings.ToLower(text)
	for _, marker := range []string{
		"<|qnml|tool_calls",
		"</|qnml|tool_calls",
		"<|qnml|invoke",
		"</|qnml|invoke",
		"<|qnml|parameter",
		"</|qnml|parameter",
		"<tool_calls",
		"</tool_calls",
		"<invoke",
		"</invoke",
		"<tool_use",
		"</tool_use",
		"##tool_call##",
		"##end_call##",
		"function.name:",
		"function.arguments:",
		"qnml|tool_calls",
		"qnml|invoke",
		"qnml|parameter",
	} {
		if strings.Contains(lowered, marker) {
			return true
		}
	}
	return false
}

var (
	missingToolContinuationLeadRe = regexp.MustCompile(`(?is)^\s*(R\d{3}\b|Round\s*\d+\b|开始|继续|下一步|接下来|现在|先|创建|写入|执行|运行|读取|检查|根据|让我|我将|我先|I will|Next|Create|Write|Run|Execute)`)
	initialToolNarrationLeadRe    = regexp.MustCompile(`(?is)^\s*(我将|我会|我先|我现在|现在|马上|开始|首先|接下来|下一步|准备|先|I will|I'll|I am going to|I’ll|Starting|Begin|First|Next|Now)\b?`)
	qnmlInvokeNameRe              = regexp.MustCompile(`(?is)<\|QNML\|invoke\s+name="([^"]+)"`)
	blockedToolNameTextRe         = regexp.MustCompile(`(?i)Tool\s+([A-Za-z0-9_.:-]+)\s+does\s+not\s+exists?\.?`)
	roundJSONRecordRe             = regexp.MustCompile(`(?is)\{[^{}]*"round"\s*:\s*"R(\d{3,4})"[^{}]*"tool"\s*:[^{}]*\}`)
	roundJSONRecordAltRe          = regexp.MustCompile(`(?is)\{[^{}]*"tool"\s*:[^{}]*"round"\s*:\s*"R(\d{3,4})"[^{}]*\}`)
)

func shouldForceToolContinuation(req StandardRequest, result CompletionResult) bool {
	if !req.ToolEnabled || len(req.Tools) == 0 || len(result.ToolCalls) > 0 {
		return false
	}
	if hasTextualToolMarker(toolParseText(result)) || isBlockedToolNameOutput(result, req.ToolNames) {
		return false
	}
	if !promptExpectsToolContinuation(req.Prompt) {
		return false
	}
	text := strings.TrimSpace(toolParseText(result))
	if text == "" {
		return true
	}
	if isLikelyCompletedToolTurn(text) {
		return shouldRejectUngroundedFinalClaim(req, result)
	}
	return isLikelyNarrationOnlyToolTurn(text)
}

func shouldRecoverMissingInitialToolCall(req StandardRequest, result CompletionResult) bool {
	if !req.ToolEnabled || len(req.Tools) == 0 || len(result.ToolCalls) > 0 {
		return false
	}
	if hasTextualToolMarker(toolParseText(result)) || isBlockedToolNameOutput(result, req.ToolNames) || isRepeatedToolCallBlockedResult(result) {
		return false
	}
	if promptExpectsToolContinuation(req.Prompt) || !promptRequiresClientToolAction(req.Prompt) {
		return false
	}
	text := strings.TrimSpace(toolParseText(result))
	if text == "" {
		return true
	}
	if isLikelyCompletedToolTurn(text) && !promptHasToolResult(req.Prompt) {
		return true
	}
	return isInitialNarrationOnlyToolTurn(text)
}

func promptRequiresClientToolAction(prompt string) bool {
	task := strings.ToLower(latestUserTaskText(prompt))
	if task == "" {
		return false
	}
	actionMarkers := []string{
		"工具调用",
		"真实工具",
		"真实执行",
		"真实检查",
		"执行一次",
		"开始执行",
		"不要只给计划",
		"全程由你操作",
		"读取",
		"读一下",
		"查看",
		"检查",
		"验证",
		"校验",
		"创建",
		"写入",
		"追加",
		"编辑",
		"修改",
		"搜索",
		"查找",
		"运行",
		"命令",
		"终端",
		"日志",
		"文件",
		"目录",
		"沙盒",
		"测试",
		"部署",
		"重启",
		"修复",
		"agent",
		"subagent",
		"skills",
		"skill",
		"wsl",
		"hermes",
		"tool call",
		"tool-call",
		"real tool",
		"execute",
		"run ",
		"shell",
		"terminal",
		"bash",
		"powershell",
		"read ",
		"write ",
		"edit ",
		"patch",
		"search",
		"grep",
		"glob",
		"browser",
		"webfetch",
		"websearch",
		"verify",
		"validate",
		"check",
		"inspect",
		"log",
		"file",
		"directory",
		"workspace",
		"repo",
	}
	for _, marker := range actionMarkers {
		if strings.Contains(task, marker) {
			return true
		}
	}
	return false
}

func latestUserTaskText(prompt string) string {
	trimmed := strings.TrimSpace(prompt)
	if trimmed == "" {
		return ""
	}
	for _, prefix := range []string{"[User]\n", "Human (ORIGINAL TASK):", "Human:"} {
		if strings.HasPrefix(trimmed, prefix) {
			task := strings.TrimSpace(trimmed[len(prefix):])
			for _, stop := range []string{"\n\nAssistant:", "\nAssistant:", "\n\n[Assistant]\n", "\n[Assistant]\n", "\n\n[Tool Result"} {
				if idx := strings.Index(task, stop); idx >= 0 {
					task = strings.TrimSpace(task[:idx])
				}
			}
			return task
		}
	}
	markers := []string{
		"\n\n[User]\n",
		"\n[User]\n",
		"\n\nHuman (ORIGINAL TASK):",
		"\n\nHuman:",
		"\nHuman:",
	}
	bestIdx := -1
	bestMarker := ""
	for _, marker := range markers {
		if idx := strings.LastIndex(trimmed, marker); idx > bestIdx {
			bestIdx = idx
			bestMarker = marker
		}
	}
	if bestIdx < 0 {
		return ""
	}
	start := bestIdx + len(bestMarker)
	task := strings.TrimSpace(trimmed[start:])
	for _, stop := range []string{"\n\nAssistant:", "\nAssistant:", "\n\n[Assistant]\n", "\n[Assistant]\n", "\n\n[Tool Result"} {
		if idx := strings.Index(task, stop); idx >= 0 {
			task = strings.TrimSpace(task[:idx])
		}
	}
	return task
}

func promptHasToolResult(prompt string) bool {
	lowered := strings.ToLower(prompt)
	return strings.Contains(prompt, "[Tool Result") ||
		strings.Contains(prompt, "[/Tool Result]") ||
		strings.Contains(lowered, "latest client message is a tool result") ||
		strings.Contains(lowered, "tool_result") ||
		strings.Contains(lowered, "function_call_output")
}

func shouldRejectUngroundedFinalClaim(req StandardRequest, result CompletionResult) bool {
	if !promptRequiresFinalEvidence(req.Prompt) {
		return false
	}
	return isMutationOnlyToolName(latestToolCallNameBeforeToolResult(req.Prompt))
}

func promptRequiresFinalEvidence(prompt string) bool {
	lowered := strings.ToLower(prompt)
	markers := []string{
		"最终校验",
		"最终检查",
		"结束前必须",
		"结束前需要",
		"必须真实检查",
		"真实检查",
		"验收",
		"校验命令",
		"检查命令",
		"final verification",
		"final check",
		"before final",
		"before finishing",
		"must verify",
		"must check",
		"verify before",
		"validate before",
		"acceptance check",
	}
	for _, marker := range markers {
		if strings.Contains(lowered, marker) {
			return true
		}
	}
	return false
}

func latestToolCallNameBeforeToolResult(prompt string) string {
	idx := strings.LastIndex(prompt, "[Tool Result")
	if idx <= 0 {
		return ""
	}
	before := prompt[:idx]
	matches := qnmlInvokeNameRe.FindAllStringSubmatch(before, -1)
	if len(matches) == 0 || len(matches[len(matches)-1]) < 2 {
		return ""
	}
	return strings.TrimSpace(html.UnescapeString(matches[len(matches)-1][1]))
}

func isMutationOnlyToolName(name string) bool {
	normalized := regexp.MustCompile(`[^a-z0-9]+`).ReplaceAllString(strings.ToLower(strings.TrimSpace(name)), "")
	switch normalized {
	case "write", "edit", "multiedit", "notebookedit", "applypatch", "patch", "createfile", "deletefile", "movefile":
		return true
	default:
		return false
	}
}

func promptExpectsToolContinuation(prompt string) bool {
	lowered := strings.ToLower(prompt)
	if strings.Contains(prompt, "[STATE NOTICE: MUST OBEY]") {
		return strings.Contains(prompt, "[Tool Result") ||
			strings.Contains(lowered, "latest client message is a tool result") ||
			strings.Contains(lowered, "latest result reports") ||
			strings.Contains(lowered, "latest tool result")
	}
	return promptHasToolResult(prompt)
}

func isLikelyCompletedToolTurn(text string) bool {
	trimmed := strings.TrimSpace(text)
	if trimmed == "" {
		return false
	}
	if looksLikeFutureToolWork(trimmed) {
		return false
	}
	lowered := strings.ToLower(trimmed)
	for _, marker := range []string{
		"final answer",
		"最终结论",
		"测试完成",
		"任务完成",
		"全部完成",
		"已全部完成",
		"总结如下",
	} {
		if strings.Contains(lowered, marker) {
			return true
		}
	}
	if strings.Contains(trimmed, "完成") && (strings.Contains(trimmed, "报告") || strings.Contains(trimmed, "任务") || strings.Contains(trimmed, "测试") || strings.Contains(trimmed, "最终")) {
		return true
	}
	return false
}

func looksLikeFutureToolWork(text string) bool {
	lowered := strings.ToLower(strings.TrimSpace(text))
	if lowered == "" {
		return false
	}
	for _, marker := range []string{
		"现在需要",
		"还需要",
		"需要继续",
		"需要推进",
		"需要创建",
		"需要写入",
		"需要更新",
		"需要检查",
		"继续执行",
		"继续推进",
		"继续：",
		"让我执行",
		"首先，",
		"接下来",
		"下一步",
		"我将",
		"我会",
		"准备",
		"need to",
		"needs to",
		"will create",
		"will write",
		"will update",
		"next,",
		"first,",
	} {
		if strings.Contains(lowered, marker) {
			return true
		}
	}
	return false
}

func isLikelyNarrationOnlyToolTurn(text string) bool {
	trimmed := strings.TrimSpace(text)
	if trimmed == "" {
		return true
	}
	runes := len([]rune(trimmed))
	lines := strings.Split(trimmed, "\n")
	nonEmpty := make([]string, 0, len(lines))
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line != "" {
			nonEmpty = append(nonEmpty, line)
		}
	}
	if len(nonEmpty) == 0 {
		return true
	}
	first := nonEmpty[0]
	if missingToolContinuationLeadRe.MatchString(first) && runes <= 1500 && len(nonEmpty) <= 20 {
		return true
	}
	return runes <= 220 && len(nonEmpty) <= 4
}

func isInitialNarrationOnlyToolTurn(text string) bool {
	trimmed := strings.TrimSpace(text)
	if trimmed == "" {
		return true
	}
	runes := len([]rune(trimmed))
	lines := strings.Split(trimmed, "\n")
	nonEmpty := make([]string, 0, len(lines))
	for _, line := range lines {
		line = strings.TrimSpace(line)
		if line != "" {
			nonEmpty = append(nonEmpty, line)
		}
	}
	if len(nonEmpty) == 0 {
		return true
	}
	if runes > 1200 || len(nonEmpty) > 16 {
		return false
	}
	first := nonEmpty[0]
	if initialToolNarrationLeadRe.MatchString(first) {
		return true
	}
	lowered := strings.ToLower(trimmed)
	planMarkers := []string{
		"我会先",
		"我将先",
		"我准备",
		"将开始",
		"开始执行",
		"开始处理",
		"先进行",
		"先创建",
		"先读取",
		"先检查",
		"先运行",
		"先执行",
		"计划如下",
		"执行计划",
		"下一步",
		"i will start",
		"i'll start",
		"i will begin",
		"i'll begin",
		"i will first",
		"i'll first",
		"starting with",
		"first, i",
		"next, i",
		"execution plan",
	}
	for _, marker := range planMarkers {
		if strings.Contains(lowered, marker) {
			return true
		}
	}
	return false
}

func extractBlockedToolNames(text string, allowedNames []string) []string {
	if strings.TrimSpace(text) == "" || !strings.Contains(strings.ToLower(text), "does not exist") {
		return nil
	}
	matches := blockedToolNameTextRe.FindAllStringSubmatch(text, -1)
	if len(matches) == 0 {
		return nil
	}
	allowed := map[string]string{}
	for _, name := range allowedNames {
		if strings.TrimSpace(name) != "" {
			allowed[strings.ToLower(name)] = name
		}
	}
	if len(allowed) == 0 {
		return nil
	}
	out := []string{}
	for _, match := range matches {
		if len(match) < 2 {
			continue
		}
		if exact, ok := allowed[strings.ToLower(match[1])]; ok {
			out = append(out, exact)
		}
	}
	return out
}

func isBlockedToolNameOutput(result CompletionResult, allowedNames []string) bool {
	if strings.HasPrefix(result.FinishReason, "blocked_tool_name:") {
		return true
	}
	return len(extractBlockedToolNames(result.AnswerText, allowedNames)) > 0 || len(extractBlockedToolNames(result.ReasoningText, allowedNames)) > 0
}

func suppressBlockedToolNameOutput(result *CompletionResult, allowedNames []string) string {
	if result == nil || len(result.ToolCalls) > 0 || !isBlockedToolNameOutput(*result, allowedNames) {
		return ""
	}
	blocked := firstNonEmpty(firstBlockedToolName(*result, allowedNames), "unknown")
	result.AnswerText = ""
	result.ReasoningText = ""
	result.FinishReason = "blocked_tool_name:" + blocked
	return blocked
}

func firstBlockedToolName(result CompletionResult, allowedNames []string) string {
	if strings.HasPrefix(result.FinishReason, "blocked_tool_name:") {
		if name := strings.TrimSpace(strings.TrimPrefix(result.FinishReason, "blocked_tool_name:")); name != "" {
			return name
		}
	}
	if blocked := extractBlockedToolNames(result.AnswerText, allowedNames); len(blocked) > 0 {
		return blocked[0]
	}
	if blocked := extractBlockedToolNames(result.ReasoningText, allowedNames); len(blocked) > 0 {
		return blocked[0]
	}
	return ""
}

func toolPromptSection(prompt string) string {
	if idx := strings.Index(prompt, "\n\nHuman:"); idx >= 0 {
		return prompt[:idx]
	}
	if idx := strings.Index(prompt, "\n\nHuman (ORIGINAL TASK):"); idx >= 0 {
		return prompt[:idx]
	}
	return prompt
}

func latestHumanLineLen(prompt string) int {
	idx := strings.LastIndex(prompt, "\n\nHuman:")
	alt := strings.LastIndex(prompt, "\n\nHuman (ORIGINAL TASK):")
	if alt > idx {
		idx = alt
	}
	if idx < 0 {
		return 0
	}
	tail := prompt[idx+2:]
	if next := strings.Index(tail, "\n\n"); next >= 0 {
		tail = tail[:next]
	}
	return len(tail)
}

// ---- migrated from qwen.go ----
const qwenBaseURL = "https://chat.qwen.ai"

type QwenClient struct {
	pool     *AccountPool
	settings Settings
	logger   *slog.Logger
	http     *http.Client
	mu       sync.Mutex
	deleted  map[string]bool
}

type UpstreamEvent struct {
	Type          string         `json:"type"`
	Phase         string         `json:"phase"`
	Content       string         `json:"content"`
	ReasoningText string         `json:"reasoning_text,omitempty"`
	Status        string         `json:"status,omitempty"`
	Extra         map[string]any `json:"extra,omitempty"`
	Raw           map[string]any `json:"raw,omitempty"`
}

type streamLineRead struct {
	line string
	err  error
}

func streamTimeoutDuration(seconds int) time.Duration {
	if seconds <= 0 {
		return 0
	}
	return time.Duration(seconds) * time.Second
}

func isMeaningfulStreamEvent(evt UpstreamEvent) bool {
	return strings.TrimSpace(evt.Content) != "" || strings.TrimSpace(evt.ReasoningText) != ""
}

func stopTimer(timer *time.Timer) {
	if timer == nil {
		return
	}
	if !timer.Stop() {
		select {
		case <-timer.C:
		default:
		}
	}
}

type TokenVerifyResult struct {
	Valid      bool
	StatusCode string
	Error      string
}

type QwenCreds struct {
	Token   string
	Cookies string
}

func qwenCreds(token, cookies string) QwenCreds {
	return QwenCreds{Token: strings.TrimSpace(token), Cookies: strings.TrimSpace(cookies)}
}

func isWAFResponse(text string) bool {
	lower := strings.ToLower(text)
	return strings.Contains(lower, "aliyun_waf") || strings.Contains(lower, "<!doctype") || strings.Contains(lower, "<html")
}

func wafBlockedError(action string) error {
	return fmt.Errorf("waf_blocked: %s 被阿里云 WAF 拦截，请通过账号激活刷新 Cookie 后重试", action)
}

func NewQwenClient(pool *AccountPool, settings Settings, logger *slog.Logger) *QwenClient {
	return &QwenClient{
		pool: pool, settings: settings, logger: logger,
		http: &http.Client{
			Transport: &http.Transport{
				Proxy: http.ProxyFromEnvironment, MaxIdleConns: 100, MaxIdleConnsPerHost: 20,
				IdleConnTimeout:       30 * time.Second,
				ResponseHeaderTimeout: streamTimeoutDuration(settings.UpstreamStreamHeaderTimeoutSeconds),
				ForceAttemptHTTP2:     true,
			},
			Timeout: 5 * time.Minute,
		},
		deleted: map[string]bool{},
	}
}

func qwenHeaders(creds QwenCreds) http.Header {
	h := http.Header{}
	if creds.Token != "" {
		h.Set("Authorization", "Bearer "+creds.Token)
	}
	h.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
	h.Set("Accept", "application/json, text/plain, */*")
	h.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
	h.Set("Referer", qwenBaseURL+"/")
	h.Set("Origin", qwenBaseURL)
	h.Set("Connection", "keep-alive")
	h.Set("sec-ch-ua", `"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"`)
	h.Set("sec-ch-ua-mobile", "?0")
	h.Set("sec-ch-ua-platform", `"Windows"`)
	h.Set("sec-fetch-dest", "empty")
	h.Set("sec-fetch-mode", "cors")
	h.Set("sec-fetch-site", "same-origin")
	if creds.Cookies != "" {
		h.Set("Cookie", creds.Cookies)
	}
	return h
}

func (c *QwenClient) requestJSON(ctx context.Context, method, path string, creds QwenCreds, body any, timeout time.Duration) (int, string, error) {
	if timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	var reader io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return 0, "", err
		}
		reader = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, qwenBaseURL+path, reader)
	if err != nil {
		return 0, "", err
	}
	req.Header = qwenHeaders(creds)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	start := time.Now()
	logInfo(c.logger, ctx, "开始上游请求", "method", method, "path", path, "token", redactToken(creds.Token), "has_cookies", creds.Cookies != "")
	resp, err := c.http.Do(req)
	if err != nil {
		logWarn(c.logger, ctx, "上游请求失败", "method", method, "path", path, "token", redactToken(creds.Token), "duration_ms", time.Since(start).Milliseconds(), "error", err)
		return 0, err.Error(), err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	attrs := []any{"method", method, "path", path, "token", redactToken(creds.Token), "status", resp.StatusCode, "bytes", len(raw), "duration_ms", time.Since(start).Milliseconds()}
	if resp.StatusCode >= 400 {
		attrs = append(attrs, "body", truncate(string(raw), 240))
		logWarn(c.logger, ctx, "上游请求完成", attrs...)
	} else {
		logInfo(c.logger, ctx, "上游请求完成", attrs...)
	}
	return resp.StatusCode, string(raw), nil
}

func (c *QwenClient) CreateChat(ctx context.Context, creds QwenCreds, model, chatType string) (string, error) {
	if chatType == "" {
		chatType = "t2t"
	}
	ts := time.Now().Unix()
	body := map[string]any{"title": fmt.Sprintf("api_%d", ts), "models": []string{model}, "chat_mode": "normal", "chat_type": normalizeUpstreamChatType(chatType), "timestamp": ts}
	logInfo(c.logger, ctx, "开始创建上游会话", "model", model, "chat_type", chatType, "token", redactToken(creds.Token), "has_cookies", creds.Cookies != "")
	status, text, err := c.requestJSON(ctx, http.MethodPost, "/api/v2/chats/new", creds, body, 30*time.Second)
	if err != nil {
		logWarn(c.logger, ctx, "创建上游会话请求失败", "model", model, "chat_type", chatType, "error", err)
		return "", err
	}
	if status != http.StatusOK {
		if isWAFResponse(text) {
			return "", wafBlockedError("create_chat")
		}
		lower := strings.ToLower(text)
		if status == 401 || status == 403 || strings.Contains(lower, "unauthorized") || strings.Contains(lower, "forbidden") || strings.Contains(lower, "token") || strings.Contains(lower, "login") {
			return "", fmt.Errorf("unauthorized: create_chat HTTP %d: %s", status, truncate(text, 200))
		}
		if status == 429 {
			return "", errors.New("429 Too Many Requests")
		}
		return "", fmt.Errorf("create_chat HTTP %d: %s", status, truncate(text, 200))
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(text), &payload); err != nil {
		if isWAFResponse(text) {
			return "", wafBlockedError("create_chat")
		}
		return "", fmt.Errorf("create_chat parse error: %w, body=%s", err, truncate(text, 200))
	}
	data, _ := payload["data"].(map[string]any)
	id, _ := data["id"].(string)
	if payload["success"] == false || id == "" {
		return "", fmt.Errorf("Qwen API returned error or missing id: %s", truncate(text, 200))
	}
	logInfo(c.logger, ctx, "创建上游会话成功", "chat_id", id, "model", model, "chat_type", chatType)
	return id, nil
}

func (c *QwenClient) DeleteChat(ctx context.Context, creds QwenCreds, chatID string) bool {
	if creds.Token == "" || chatID == "" {
		return true
	}
	c.mu.Lock()
	if c.deleted[chatID] {
		c.mu.Unlock()
		return true
	}
	c.mu.Unlock()
	for attempt := 1; attempt <= max(1, c.settings.ChatDeleteRetryAttempts); attempt++ {
		status, text, err := c.requestJSON(ctx, http.MethodDelete, "/api/v2/chats/"+chatID, creds, nil, 20*time.Second)
		if err == nil && (status == 200 || status == 204 || status == 404) {
			c.mu.Lock()
			c.deleted[chatID] = true
			c.mu.Unlock()
			logInfo(c.logger, ctx, "删除上游会话完成", "chat_id", chatID, "attempt", attempt, "status", status)
			return true
		}
		logWarn(c.logger, ctx, "删除上游会话失败", "chat_id", chatID, "attempt", attempt, "status", status, "error", err, "body", truncate(text, 120))
		time.Sleep(time.Duration(c.settings.ChatDeleteRetryDelaySeconds*float64(attempt)*1000) * time.Millisecond)
	}
	return false
}

func (c *QwenClient) StreamChat(ctx context.Context, creds QwenCreds, chatID string, payload map[string]any, onEvent func(UpstreamEvent) error) error {
	raw, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	streamCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	req, err := http.NewRequestWithContext(streamCtx, http.MethodPost, qwenBaseURL+"/api/v2/chat/completions?chat_id="+chatID, bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header = qwenHeaders(creds)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")
	logInfo(c.logger, ctx, "开始上游流式请求", "chat_id", chatID, "token", redactToken(creds.Token), "has_cookies", creds.Cookies != "", "payload_bytes", len(raw))
	start := time.Now()
	resp, err := c.http.Do(req)
	if err != nil {
		logWarn(c.logger, ctx, "上游流式请求失败", "chat_id", chatID, "duration_ms", time.Since(start).Milliseconds(), "error", err)
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		logWarn(c.logger, ctx, "上游流式返回错误", "chat_id", chatID, "status", resp.StatusCode, "duration_ms", time.Since(start).Milliseconds(), "body", truncate(string(body), 240))
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, truncate(string(body), 800))
	}
	firstEventTimeout := streamTimeoutDuration(c.settings.UpstreamStreamFirstEventTimeoutSeconds)
	idleTimeout := streamTimeoutDuration(c.settings.UpstreamStreamIdleTimeoutSeconds)
	logInfo(c.logger, ctx, "上游流式连接建立", "chat_id", chatID, "status", resp.StatusCode, "first_event_timeout_seconds", int(firstEventTimeout/time.Second), "idle_timeout_seconds", int(idleTimeout/time.Second))
	reader := bufio.NewReader(resp.Body)
	lineReads := make(chan streamLineRead, 1)
	go func() {
		for {
			line, readErr := reader.ReadString('\n')
			select {
			case lineReads <- streamLineRead{line: line, err: readErr}:
			case <-streamCtx.Done():
				return
			}
			if readErr != nil {
				return
			}
		}
	}()
	var block strings.Builder
	events := 0
	firstEventLogged := false
	rawTail := ""
	totalBytes := 0
	var firstEventTimer *time.Timer
	var firstEventCh <-chan time.Time
	if firstEventTimeout > 0 {
		firstEventTimer = time.NewTimer(firstEventTimeout)
		firstEventCh = firstEventTimer.C
		defer stopTimer(firstEventTimer)
	}
	var idleTimer *time.Timer
	var idleCh <-chan time.Time
	defer stopTimer(idleTimer)
	resetIdleTimer := func() {
		if idleTimeout <= 0 {
			return
		}
		if idleTimer == nil {
			idleTimer = time.NewTimer(idleTimeout)
			idleCh = idleTimer.C
			return
		}
		stopTimer(idleTimer)
		idleTimer.Reset(idleTimeout)
	}
	parseBlock := func(blockText string) error {
		if upstreamError := upstream.ExtractUpstreamError(blockText); upstreamError != "" {
			return errors.New(upstreamError)
		}
		return parseSSEBlock(blockText, func(evt UpstreamEvent) error {
			events++
			if !firstEventLogged {
				firstEventLogged = true
				stopTimer(firstEventTimer)
				firstEventCh = nil
				logInfo(c.logger, ctx, "上游首次事件", "chat_id", chatID, "after_ms", time.Since(start).Milliseconds(), "event_type", firstNonEmpty(evt.Type, "-"), "phase", firstNonEmpty(evt.Phase, "-"), "status", firstNonEmpty(evt.Status, "-"))
				resetIdleTimer()
			}
			if isMeaningfulStreamEvent(evt) {
				resetIdleTimer()
			}
			return onEvent(evt)
		})
	}
	for {
		select {
		case read := <-lineReads:
			line, err := read.line, read.err
			if line != "" {
				totalBytes += len(line)
				rawTail = (rawTail + line)
				if len(rawTail) > 500 {
					rawTail = rawTail[len(rawTail)-500:]
				}
				line = strings.TrimRight(line, "\r\n")
				if strings.TrimSpace(line) == "" {
					if block.Len() > 0 {
						if err := parseBlock(block.String()); err != nil {
							return err
						}
						block.Reset()
					}
				} else {
					block.WriteString(line)
					block.WriteByte('\n')
				}
			}
			if err != nil {
				if errors.Is(err, io.EOF) {
					if block.Len() > 0 {
						if err := parseBlock(block.String()); err != nil {
							return err
						}
					}
					logInfo(c.logger, ctx, "上游流式读取完成", "chat_id", chatID, "events", events, "duration_ms", time.Since(start).Milliseconds(), "stream_bytes", totalBytes)
					if events == 0 {
						if upstreamError := upstream.ExtractUpstreamError(rawTail); upstreamError != "" {
							return errors.New(upstreamError)
						}
						logWarn(c.logger, ctx, "上游 SSE 未解析到有效 delta", "chat_id", chatID, "stream_bytes", totalBytes, "raw_tail", truncate(rawTail, 500))
					}
					return nil
				}
				return err
			}
		case <-firstEventCh:
			cancel()
			logWarn(c.logger, ctx, "上游流式首事件超时", "chat_id", chatID, "timeout_seconds", int(firstEventTimeout/time.Second), "duration_ms", time.Since(start).Milliseconds(), "stream_bytes", totalBytes, "raw_tail", truncate(rawTail, 500))
			return fmt.Errorf("upstream stream first event timeout after %s without parsed SSE event", firstEventTimeout)
		case <-idleCh:
			cancel()
			logWarn(c.logger, ctx, "上游流式空闲超时", "chat_id", chatID, "timeout_seconds", int(idleTimeout/time.Second), "events", events, "duration_ms", time.Since(start).Milliseconds(), "stream_bytes", totalBytes, "raw_tail", truncate(rawTail, 500))
			return fmt.Errorf("upstream stream idle timeout after %s without parsed SSE event", idleTimeout)
		case <-streamCtx.Done():
			return streamCtx.Err()
		}
	}
}

func (c *QwenClient) PostChatCompletionOnce(ctx context.Context, creds QwenCreds, chatID string, payload map[string]any, timeout time.Duration) (int, string, error) {
	if timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return 0, "", err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, qwenBaseURL+"/api/v2/chat/completions?chat_id="+chatID, bytes.NewReader(raw))
	if err != nil {
		return 0, "", err
	}
	req.Header = qwenHeaders(creds)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Accel-Buffering", "no")
	logInfo(c.logger, ctx, "开始上游非流式请求", "chat_id", chatID, "token", redactToken(creds.Token), "has_cookies", creds.Cookies != "", "payload_bytes", len(raw))
	start := time.Now()
	resp, err := c.http.Do(req)
	if err != nil {
		logWarn(c.logger, ctx, "上游非流式请求失败", "chat_id", chatID, "duration_ms", time.Since(start).Milliseconds(), "error", err)
		return 0, err.Error(), err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	attrs := []any{"chat_id", chatID, "status", resp.StatusCode, "bytes", len(body), "duration_ms", time.Since(start).Milliseconds()}
	if resp.StatusCode >= 400 {
		attrs = append(attrs, "body", truncate(string(body), 240))
		logWarn(c.logger, ctx, "上游非流式请求完成", attrs...)
	} else {
		logInfo(c.logger, ctx, "上游非流式请求完成", attrs...)
	}
	return resp.StatusCode, string(body), nil
}

func (c *QwenClient) GetVisionTaskStatus(ctx context.Context, creds QwenCreds, taskID string, timeout time.Duration) (int, string, error) {
	logInfo(c.logger, ctx, "查询上游视觉任务", "task_id", taskID)
	return c.requestJSON(ctx, http.MethodGet, "/api/v1/tasks/status/"+taskID, creds, nil, timeout)
}

func (c *QwenClient) GetChatDetail(ctx context.Context, creds QwenCreds, chatID string, timeout time.Duration) (int, string, error) {
	logInfo(c.logger, ctx, "查询上游会话详情", "chat_id", chatID)
	return c.requestJSON(ctx, http.MethodGet, "/api/v2/chats/"+chatID, creds, nil, timeout)
}

func (c *QwenClient) ListChats(ctx context.Context, creds QwenCreds, limit int) ([]map[string]any, error) {
	if limit <= 0 {
		limit = 50
	}
	logInfo(c.logger, ctx, "查询上游会话列表", "limit", limit)
	status, text, err := c.requestJSON(ctx, http.MethodGet, fmt.Sprintf("/api/v2/chats?limit=%d", limit), creds, nil, 20*time.Second)
	if err != nil {
		logWarn(c.logger, ctx, "查询上游会话列表失败", "limit", limit, "error", err)
		return nil, err
	}
	if status != http.StatusOK {
		logWarn(c.logger, ctx, "查询上游会话列表状态异常", "limit", limit, "status", status, "body", truncate(text, 240))
		return nil, fmt.Errorf("list_chats HTTP %d: %s", status, truncate(text, 200))
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(text), &payload); err != nil {
		logWarn(c.logger, ctx, "解析上游会话列表失败", "limit", limit, "error", err)
		return nil, err
	}
	items := mapList(payload["data"])
	logInfo(c.logger, ctx, "查询上游会话列表完成", "limit", limit, "count", len(items))
	return items, nil
}

func (c *QwenClient) ListModelsFromPool(ctx context.Context) ([]map[string]any, error) {
	if !c.pool.HasAvailableFor(accountUsageMetadata) {
		logWarn(c.logger, ctx, "拉取上游模型失败", "error", "no available upstream account")
		return nil, errors.New("no available upstream account")
	}
	acc, err := c.pool.AcquireFor(ctx, "", accountUsageMetadata)
	if err != nil {
		logWarn(c.logger, ctx, "拉取上游模型获取账号失败", "error", err)
		return nil, err
	}
	defer c.pool.Release(acc)
	setRequestLogFields(ctx, "account", acc.Email)
	logInfo(c.logger, ctx, "拉取上游模型", "account", acc.Email)
	status, text, err := c.requestJSON(ctx, http.MethodGet, "/api/models", qwenCreds(acc.Token, acc.Cookies), nil, 20*time.Second)
	if err != nil || status != 200 {
		if err != nil {
			logWarn(c.logger, ctx, "拉取上游模型请求失败", "account", acc.Email, "error", err)
			return nil, err
		} else {
			logWarn(c.logger, ctx, "拉取上游模型状态异常", "account", acc.Email, "status", status, "body", truncate(text, 240))
			return nil, fmt.Errorf("list_models HTTP %d: %s", status, truncate(text, 200))
		}
	}
	var decoded any
	if err := json.Unmarshal([]byte(text), &decoded); err != nil {
		logWarn(c.logger, ctx, "解析上游模型失败", "account", acc.Email, "error", err)
		return nil, err
	}
	models := extractModelList(decoded)
	logInfo(c.logger, ctx, "拉取上游模型完成", "account", acc.Email, "count", len(models))
	return models, nil
}

func (c *QwenClient) VerifyToken(ctx context.Context, creds QwenCreds) bool {
	return c.VerifyTokenDetail(ctx, creds).Valid
}

func (c *QwenClient) VerifyTokenDetail(ctx context.Context, creds QwenCreds) TokenVerifyResult {
	if creds.Token == "" {
		logWarn(c.logger, ctx, "账号 Token 验证失败", "status_code", "auth_error", "error", "empty token")
		return TokenVerifyResult{StatusCode: "auth_error", Error: "empty token"}
	}
	logInfo(c.logger, ctx, "开始账号 Token 验证", "token", redactToken(creds.Token), "has_cookies", creds.Cookies != "")
	status, text, err := c.requestJSON(ctx, http.MethodGet, "/api/v2/user/info", creds, nil, 20*time.Second)
	if err != nil {
		logWarn(c.logger, ctx, "账号 Token 验证请求失败", "token", redactToken(creds.Token), "error", err)
		return TokenVerifyResult{StatusCode: "network_error", Error: err.Error()}
	}
	lower := strings.ToLower(text)
	if status >= 200 && status < 300 && !strings.Contains(lower, "unauthorized") && !isWAFResponse(text) {
		logInfo(c.logger, ctx, "账号 Token 验证通过", "token", redactToken(creds.Token), "status", status)
		return TokenVerifyResult{Valid: true, StatusCode: "valid"}
	}
	statusCode := "invalid"
	switch {
	case isWAFResponse(text):
		statusCode = "waf_blocked"
	case status == 401 || status == 403 || strings.Contains(lower, "unauthorized") || strings.Contains(lower, "forbidden") || strings.Contains(lower, "login") || strings.Contains(lower, "token"):
		statusCode = "auth_error"
	case status == 429:
		statusCode = "rate_limited"
	case strings.Contains(lower, "ban") || strings.Contains(lower, "disabled"):
		statusCode = "banned"
	}
	result := TokenVerifyResult{StatusCode: statusCode, Error: fmt.Sprintf("HTTP %d: %s", status, truncate(text, 200))}
	logWarn(c.logger, ctx, "账号 Token 验证失败", "token", redactToken(creds.Token), "status", status, "status_code", statusCode, "body", truncate(text, 240))
	return result
}

func parseSSEBlock(block string, onEvent func(UpstreamEvent) error) error {
	for _, line := range strings.Split(block, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		if data == "" || data == "[DONE]" {
			continue
		}
		var obj map[string]any
		if json.Unmarshal([]byte(data), &obj) != nil {
			continue
		}
		for _, evt := range parseQwenEvent(obj) {
			if err := onEvent(evt); err != nil {
				return err
			}
		}
	}
	return nil
}

func parseQwenEvent(obj map[string]any) []UpstreamEvent {
	parsed := upstream.ParseQwenEvent(obj)
	if len(parsed) == 0 {
		return nil
	}
	events := make([]UpstreamEvent, 0, len(parsed))
	for _, evt := range parsed {
		events = append(events, UpstreamEvent{
			Type:          evt.Type,
			Phase:         evt.Phase,
			Content:       evt.Content,
			ReasoningText: evt.ReasoningText,
			Status:        evt.Status,
			Extra:         evt.Extra,
			Raw:           evt.Raw,
		})
	}
	return events
}

func firstString(values ...any) string {
	for _, value := range values {
		if s, ok := value.(string); ok && s != "" {
			return s
		}
	}
	return ""
}

// ---- migrated from request_logging.go ----
var logTestMarkers = []string{
	"TEST_DELETE_20260601",
	"TEST_LONG_INPUT_20260601",
}

type requestLogContextKey struct{}

type requestLogContext struct {
	mu             sync.Mutex
	ReqID          string
	Surface        string
	RequestedModel string
	ResolvedModel  string
	ChatID         string
	Account        string
	Stream         string
	ToolEnabled    string
	TestMarker     string
	PromptLen      int
	Start          time.Time
}

type loggingResponseWriter struct {
	http.ResponseWriter
	status int
	bytes  int
}

func (w *loggingResponseWriter) WriteHeader(status int) {
	if w.status != 0 {
		return
	}
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}

func (w *loggingResponseWriter) Write(data []byte) (int, error) {
	if w.status == 0 {
		w.WriteHeader(http.StatusOK)
	}
	n, err := w.ResponseWriter.Write(data)
	w.bytes += n
	return n, err
}

func (w *loggingResponseWriter) Flush() {
	if flusher, ok := w.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (w *loggingResponseWriter) Unwrap() http.ResponseWriter {
	return w.ResponseWriter
}

func (app *App) withRequestLogging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reqID := strings.TrimSpace(r.Header.Get("X-Request-ID"))
		if reqID == "" {
			reqID = randomID()[:8]
		}
		logCtx := &requestLogContext{
			ReqID:       reqID,
			Surface:     requestSurface(r.URL.Path),
			Stream:      "-",
			ToolEnabled: "-",
			TestMarker:  "-",
			Start:       time.Now(),
		}
		ctx := context.WithValue(r.Context(), requestLogContextKey{}, logCtx)
		r = r.WithContext(ctx)

		w.Header().Set("X-Request-ID", reqID)
		recorder := &loggingResponseWriter{ResponseWriter: w}

		app.logInfo(ctx, "请求进入",
			"method", r.Method,
			"path", r.URL.Path,
			"query", truncate(r.URL.RawQuery, 240),
			"remote", r.RemoteAddr,
			"user_agent", truncate(r.UserAgent(), 160),
		)

		defer func() {
			if recovered := recover(); recovered != nil {
				app.logError(ctx, "请求处理异常",
					"panic", recovered,
					"stack", string(debug.Stack()),
				)
				writeError(recorder, http.StatusInternalServerError, "internal server error")
			}
			status := recorder.status
			if status == 0 {
				status = http.StatusOK
			}
			attrs := []any{
				"method", r.Method,
				"path", r.URL.Path,
				"status", status,
				"bytes", recorder.bytes,
				"duration_ms", time.Since(logCtx.Start).Milliseconds(),
			}
			if status >= 500 {
				app.logError(ctx, "请求完成", attrs...)
			} else if status >= 400 {
				app.logWarn(ctx, "请求完成", attrs...)
			} else {
				app.logInfo(ctx, "请求完成", attrs...)
			}
		}()

		next.ServeHTTP(recorder, r)
	})
}

func requestSurface(path string) string {
	switch {
	case path == "/healthz" || path == "/readyz" || strings.HasPrefix(path, "/admin/dev/"):
		return "probe"
	case strings.HasPrefix(path, "/api/admin/"):
		return "admin"
	case strings.Contains(path, "/images/"):
		return "images"
	case strings.Contains(path, "/videos/"):
		return "videos"
	case strings.Contains(path, "/files"):
		return "files"
	case strings.Contains(path, "/messages"):
		return "anthropic"
	case strings.Contains(path, "generateContent"):
		return "gemini"
	case strings.Contains(path, "/responses"):
		return "responses"
	case strings.Contains(path, "/embeddings"):
		return "embeddings"
	case strings.Contains(path, "/chat/completions"):
		return "openai"
	default:
		return "system"
	}
}

func setRequestLogFields(ctx context.Context, fields ...any) {
	info := requestLogFromContext(ctx)
	if info == nil {
		return
	}
	info.mu.Lock()
	defer info.mu.Unlock()
	for i := 0; i+1 < len(fields); i += 2 {
		key, ok := fields[i].(string)
		if !ok {
			continue
		}
		switch key {
		case "surface":
			info.Surface = anyString(fields[i+1], info.Surface)
		case "requested_model":
			info.RequestedModel = anyString(fields[i+1], info.RequestedModel)
		case "resolved_model":
			info.ResolvedModel = anyString(fields[i+1], info.ResolvedModel)
		case "chat_id":
			info.ChatID = anyString(fields[i+1], info.ChatID)
		case "account":
			info.Account = anyString(fields[i+1], info.Account)
		case "stream":
			info.Stream = anyString(fields[i+1], info.Stream)
		case "tool_enabled":
			info.ToolEnabled = anyString(fields[i+1], info.ToolEnabled)
		case "test_marker":
			info.TestMarker = anyString(fields[i+1], info.TestMarker)
		case "prompt_len":
			if value, ok := fields[i+1].(int); ok {
				info.PromptLen = value
			}
		}
	}
}

func requestLogFromContext(ctx context.Context) *requestLogContext {
	if ctx == nil {
		return nil
	}
	info, _ := ctx.Value(requestLogContextKey{}).(*requestLogContext)
	return info
}

func requestLogAttrs(ctx context.Context) []any {
	info := requestLogFromContext(ctx)
	if info == nil {
		return nil
	}
	info.mu.Lock()
	defer info.mu.Unlock()
	attrs := []any{
		"req_id", info.ReqID,
		"surface", info.Surface,
	}
	if info.RequestedModel != "" {
		attrs = append(attrs, "requested_model", info.RequestedModel)
	}
	if info.ResolvedModel != "" {
		attrs = append(attrs, "resolved_model", info.ResolvedModel)
	}
	if info.ChatID != "" {
		attrs = append(attrs, "chat_id", info.ChatID)
	}
	if info.Account != "" {
		attrs = append(attrs, "account", info.Account)
	}
	if info.Stream != "" {
		attrs = append(attrs, "stream", info.Stream)
	}
	if info.ToolEnabled != "" {
		attrs = append(attrs, "tool_enabled", info.ToolEnabled)
	}
	if info.TestMarker != "" {
		attrs = append(attrs, "test_marker", info.TestMarker)
	}
	if info.PromptLen > 0 {
		attrs = append(attrs, "prompt_len", info.PromptLen)
	}
	return attrs
}

func appendLogAttrs(ctx context.Context, attrs ...any) []any {
	base := requestLogAttrs(ctx)
	out := make([]any, 0, len(base)+len(attrs))
	out = append(out, base...)
	out = append(out, attrs...)
	return out
}

func (app *App) logInfo(ctx context.Context, msg string, attrs ...any) {
	if app != nil && app.logger != nil {
		app.logger.Info(msg, appendLogAttrs(ctx, attrs...)...)
	}
}

func (app *App) logWarn(ctx context.Context, msg string, attrs ...any) {
	if app != nil && app.logger != nil {
		app.logger.Warn(msg, appendLogAttrs(ctx, attrs...)...)
	}
}

func (app *App) logError(ctx context.Context, msg string, attrs ...any) {
	if app != nil && app.logger != nil {
		app.logger.Error(msg, appendLogAttrs(ctx, attrs...)...)
	}
}

func logInfo(logger *slog.Logger, ctx context.Context, msg string, attrs ...any) {
	if logger != nil {
		logger.Info(msg, appendLogAttrs(ctx, attrs...)...)
	}
}

func logWarn(logger *slog.Logger, ctx context.Context, msg string, attrs ...any) {
	if logger != nil {
		logger.Warn(msg, appendLogAttrs(ctx, attrs...)...)
	}
}

func logError(logger *slog.Logger, ctx context.Context, msg string, attrs ...any) {
	if logger != nil {
		logger.Error(msg, appendLogAttrs(ctx, attrs...)...)
	}
}

func boolLogValue(value bool) string {
	if value {
		return "true"
	}
	return "false"
}

func redactToken(token string) string {
	token = strings.TrimSpace(token)
	if token == "" {
		return "-"
	}
	if len(token) <= 12 {
		return "token-hidden"
	}
	return token[:6] + "..." + token[len(token)-4:]
}

func findLogTestMarkers(text string) []string {
	markers := []string{}
	for _, marker := range logTestMarkers {
		if strings.Contains(text, marker) {
			markers = append(markers, marker)
		}
	}
	return markers
}

func promptSHA256(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}

func promptTail(text string, limit int) string {
	if limit <= 0 || len(text) <= limit {
		return text
	}
	return text[len(text)-limit:]
}

// ---- migrated from store.go ----
type JSONStore struct {
	path        string
	defaultData any
	mu          sync.RWMutex
}

func NewJSONStore(path string, defaultData any) *JSONStore {
	return &JSONStore{path: path, defaultData: defaultData}
}

func (s *JSONStore) Path() string { return s.path }

func (s *JSONStore) Ensure() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, err := os.Stat(s.path); err == nil {
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return writeJSONFileLocked(s.path, s.defaultData)
}

func (s *JSONStore) LoadInto(v any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, err := os.Stat(s.path); errors.Is(err, os.ErrNotExist) {
		if err := writeJSONFileLocked(s.path, s.defaultData); err != nil {
			return err
		}
	}
	raw, err := os.ReadFile(s.path)
	if err != nil {
		return err
	}
	if len(raw) == 0 {
		raw, _ = json.Marshal(s.defaultData)
	}
	return json.Unmarshal(raw, v)
}

func (s *JSONStore) LoadAny() (any, error) {
	var v any
	if err := s.LoadInto(&v); err != nil {
		return nil, err
	}
	return v, nil
}

func (s *JSONStore) Save(v any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return writeJSONFileLocked(s.path, v)
}

func writeJSONFile(path string, v any) error {
	return writeJSONFileLocked(path, v)
}

func writeJSONFileLocked(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// ---- migrated from tool_parser.go ----
type ParsedToolCall = toolcall.ParsedToolCall

func parseToolCalls(text string, tools []map[string]any) []ParsedToolCall {
	return toolcall.ParseToolCalls(text, tools)
}

func parseXMLToolCalls(text string, allowed map[string]string) []ParsedToolCall {
	calls := []ParsedToolCall{}
	patterns := []*regexp.Regexp{
		regexp.MustCompile(`(?is)<tool_use\b[^>]*\bname=["']([^"']+)["'][^>]*>(.*?)</tool_use>`),
		regexp.MustCompile(`(?is)<tool_call\b[^>]*\bname=["']([^"']+)["'][^>]*>(.*?)</tool_call>`),
		regexp.MustCompile(`(?is)<function\b[^>]*\bname=["']([^"']+)["'][^>]*>(.*?)</function>`),
	}
	for _, re := range patterns {
		for _, match := range re.FindAllStringSubmatch(text, -1) {
			if len(match) < 3 {
				continue
			}
			name := canonicalToolName(match[1], allowed)
			if name == "" {
				continue
			}
			input := parseToolInput(strings.TrimSpace(match[2]))
			calls = append(calls, ParsedToolCall{ID: "call_" + randomID()[:12], Name: name, Input: input})
		}
	}
	return calls
}

func parseJSONToolCalls(value any, allowed map[string]string) []ParsedToolCall {
	calls := []ParsedToolCall{}
	switch v := value.(type) {
	case map[string]any:
		if rawList, ok := v["tool_calls"].([]any); ok {
			for _, raw := range rawList {
				calls = append(calls, parseJSONToolCalls(raw, allowed)...)
			}
		}
		if rawList, ok := v["tools"].([]any); ok {
			for _, raw := range rawList {
				calls = append(calls, parseJSONToolCalls(raw, allowed)...)
			}
		}
		name := firstString(v["name"], v["tool"], v["tool_name"], v["function_name"])
		input := firstNonNil(v["input"], v["arguments"], v["args"], v["parameters"])
		if fn, ok := v["function"].(map[string]any); ok {
			if name == "" {
				name = firstString(fn["name"])
			}
			if input == nil {
				input = firstNonNil(fn["arguments"], fn["input"], fn["parameters"])
			}
		}
		if name = canonicalToolName(name, allowed); name != "" {
			calls = append(calls, ParsedToolCall{ID: firstNonEmpty(firstString(v["id"], v["call_id"]), "call_"+randomID()[:12]), Name: name, Input: normalizeToolInput(input)})
		}
	case []any:
		for _, item := range v {
			calls = append(calls, parseJSONToolCalls(item, allowed)...)
		}
	}
	return calls
}

func canonicalToolName(name string, allowed map[string]string) string {
	name = strings.TrimSpace(name)
	if name == "" {
		return ""
	}
	if exact, ok := allowed[strings.ToLower(name)]; ok {
		return exact
	}
	return ""
}

func parseToolInput(text string) any {
	if text == "" {
		return map[string]any{}
	}
	var value any
	if err := json.Unmarshal([]byte(text), &value); err == nil {
		return normalizeToolInput(value)
	}
	params := map[string]any{}
	re := regexp.MustCompile(`(?is)<([A-Za-z_][A-Za-z0-9_\-]*)>(.*?)</\1>`)
	for _, match := range re.FindAllStringSubmatch(text, -1) {
		if len(match) == 3 {
			params[match[1]] = strings.TrimSpace(match[2])
		}
	}
	if len(params) > 0 {
		return params
	}
	return map[string]any{"input": text}
}

func normalizeToolInput(value any) any {
	switch v := value.(type) {
	case nil:
		return map[string]any{}
	case string:
		var decoded any
		if err := json.Unmarshal([]byte(v), &decoded); err == nil {
			return normalizeToolInput(decoded)
		}
		return v
	default:
		return v
	}
}

func dedupeToolCalls(calls []ParsedToolCall) []ParsedToolCall {
	return toolcall.DedupeToolCalls(calls)
}

func openAIToolCalls(calls []ParsedToolCall) []map[string]any {
	return toolcall.OpenAIToolCalls(calls)
}

func responsesToolItems(calls []ParsedToolCall) []map[string]any {
	return toolcall.ResponsesToolItems(calls)
}

// ---- migrated from upstream_payload.go ----
func normalizeUpstreamChatType(chatType string) string {
	return upstream.NormalizeChatType(chatType)
}

func buildChatPayload(chatID, model, content string, hasCustomTools bool, files []map[string]any, chatType string, imageOptions map[string]any, thinkingEnabled *bool, enableSearch bool) map[string]any {
	return upstream.BuildChatPayload(chatID, model, content, hasCustomTools, files, chatType, imageOptions, thinkingEnabled, enableSearch)
}

func imageRatio(options map[string]any) string {
	for _, key := range []string{"ratio", "aspect_ratio", "aspectRatio"} {
		if v, ok := options[key].(string); ok && v != "" {
			return v
		}
	}
	return "1:1"
}

// ---- migrated from util.go ----
func normalizeLower(s string) string { return strings.ToLower(strings.TrimSpace(s)) }

func randomID() string {
	buf := make([]byte, 16)
	if _, err := cryptorand.Read(buf); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(buf)
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

func writeError(w http.ResponseWriter, status int, detail any) {
	writeJSON(w, status, map[string]any{"detail": detail})
}

func decodeJSON(r *http.Request, dst any) error {
	defer r.Body.Close()
	dec := json.NewDecoder(io.LimitReader(r.Body, 256<<20))
	dec.UseNumber()
	return dec.Decode(dst)
}

func stringValue(m map[string]any, key, fallback string) string {
	if m == nil {
		return fallback
	}
	v, ok := m[key]
	if !ok {
		return fallback
	}
	return anyString(v, fallback)
}

func anyString(v any, fallback string) string {
	switch x := v.(type) {
	case string:
		if x != "" {
			return x
		}
	case json.Number:
		return x.String()
	case fmt.Stringer:
		return x.String()
	}
	return fallback
}

func intValue(m map[string]any, key string, fallback int) int {
	v, ok := m[key]
	if !ok {
		return fallback
	}
	switch x := v.(type) {
	case int:
		return x
	case float64:
		return int(x)
	case json.Number:
		if i, err := strconv.Atoi(x.String()); err == nil {
			return i
		}
	case string:
		if i, err := strconv.Atoi(strings.TrimSpace(x)); err == nil {
			return i
		}
	}
	return fallback
}

func boolValue(v any) bool {
	b, _ := v.(bool)
	return b
}

func coerceBool(v any) *bool {
	switch x := v.(type) {
	case bool:
		return &x
	case float64:
		b := x != 0
		return &b
	case json.Number:
		i, _ := strconv.Atoi(x.String())
		b := i != 0
		return &b
	case string:
		switch normalizeLower(x) {
		case "1", "true", "yes", "on", "enable", "enabled", "auto", "thinking":
			b := true
			return &b
		case "0", "false", "no", "off", "disable", "disabled", "fast", "none":
			b := false
			return &b
		}
	}
	return nil
}

func anyList(v any) []any {
	if list, ok := v.([]any); ok {
		return list
	}
	return nil
}

func firstStringAny(values ...any) string {
	for _, v := range values {
		if s, ok := v.(string); ok && strings.TrimSpace(s) != "" {
			return strings.TrimSpace(s)
		}
	}
	return ""
}

func extractModelList(decoded any) []map[string]any {
	switch v := decoded.(type) {
	case []any:
		return mapList(v)
	case map[string]any:
		if out := mapList(v["data"]); len(out) > 0 {
			return out
		}
		if out := mapList(v["models"]); len(out) > 0 {
			return out
		}
	}
	return nil
}

func mapList(v any) []map[string]any {
	raw, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]map[string]any, 0, len(raw))
	for _, item := range raw {
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

func pseudoEmbedding(text string) []float64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(text))
	base := float64(h.Sum64()%math.MaxUint32) / float64(math.MaxUint32)
	vec := make([]float64, 1536)
	for i := range vec {
		vec[i] = (base*float64(i%10))/10.0 - 0.5
	}
	return vec
}

func splitExts(value string) map[string]bool {
	out := map[string]bool{}
	for _, item := range strings.Split(value, ",") {
		item = strings.Trim(strings.ToLower(item), " .")
		if item != "" {
			out[item] = true
		}
	}
	return out
}

func fileExt(name string) string {
	return strings.TrimPrefix(strings.ToLower(filepath.Ext(name)), ".")
}

func truncate(text string, limit int) string {
	text = strings.TrimSpace(text)
	if limit <= 0 || len(text) <= limit {
		return text
	}
	return text[:limit]
}

func trim(text string, limit int) string {
	return truncate(text, limit)
}
