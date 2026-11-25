package analysis

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"sync/atomic"
	"time"
)

type Context struct {
	SchemaVersion int             `json:"schema_version"`
	AnalysisType  string          `json:"analysis_type"`
	Currency      string          `json:"currency"`
	Period        string          `json:"period"`
	DataStatus    string          `json:"data_status"`
	Generation    int             `json:"generation"`
	SnapshotID    string          `json:"snapshot_id"`
	Reporting     ReportingPeriod `json:"reporting_period"`
	Metrics       Metrics         `json:"metrics"`
	Facts         []Fact          `json:"facts"`
	Evidence      []Evidence      `json:"evidence"`
	Freshness     Freshness       `json:"freshness"`
	Limitations   []string        `json:"limitations"`
	GeneratedAt   string          `json:"generated_at"`
}

type ReportingPeriod struct {
	Kind     string  `json:"kind"`
	Start    *string `json:"start"`
	End      *string `json:"end"`
	Timezone string  `json:"timezone"`
}

type Metrics struct {
	TotalIncome            string  `json:"total_income"`
	TotalExpense           string  `json:"total_expense"`
	Balance                string  `json:"balance"`
	NetWorth               string  `json:"net_worth"`
	AccountCount           int     `json:"account_count"`
	ExpenseToIncomePercent *string `json:"expense_to_income_percent"`
}

type Fact struct {
	ID           string  `json:"id"`
	Metric       string  `json:"metric"`
	ValueType    string  `json:"value_type"`
	Value        *string `json:"value"`
	Unit         string  `json:"unit"`
	Period       string  `json:"period"`
	Availability string  `json:"availability"`
	Definition   string  `json:"definition"`
}

type Evidence struct {
	ID         string `json:"id"`
	SnapshotID string `json:"snapshot_id"`
	FactID     string `json:"fact_id"`
	Source     string `json:"source"`
}

type Freshness struct {
	Status               string `json:"status"`
	Generation           int    `json:"generation"`
	CanonicalFingerprint string `json:"canonical_fingerprint"`
	ProjectionRebuiltAt  string `json:"projection_rebuilt_at"`
	CheckedAt            string `json:"checked_at"`
}

type Finding struct {
	Metric      string   `json:"metric"`
	Description string   `json:"description"`
	Evidence    []string `json:"evidence"`
}

type Result struct {
	Summary     string    `json:"summary"`
	Findings    []Finding `json:"findings"`
	Limitations []string  `json:"limitations"`
}

const OutputSchemaVersion = 1

const (
	MaxIncomingRequestBytes = 64 << 10
	MaxModelOutputBytes     = 8 << 10
	MaxFindings             = 6
)

type MetricKey string

type ClaimCode string

const (
	ClaimValueReported            ClaimCode = "VALUE_REPORTED"
	ClaimRatioUndefinedZeroIncome ClaimCode = "RATIO_UNDEFINED_ZERO_INCOME"
)

type ModelOutput struct {
	OutputSchemaVersion int            `json:"output_schema_version"`
	SnapshotID          string         `json:"snapshot_id"`
	Findings            []ModelFinding `json:"findings"`
}

type ModelFinding struct {
	Metric     MetricKey `json:"metric"`
	FactID     string    `json:"fact_id"`
	EvidenceID string    `json:"evidence_id"`
	ClaimCode  ClaimCode `json:"claim_code"`
}

type Model interface {
	Explain(ctx context.Context, c Context) ([]byte, error)
}

const (
	ProviderConcurrencyLimit = 10
	AnalysisTimeout          = 8 * time.Second
	ProviderTimeout          = 7 * time.Second
)

type MetricsSnapshot struct {
	InFlight               int64
	OverloadRejections     uint64
	Timeouts               uint64
	Cancellations          uint64
	ProviderLatencySamples uint64
	ProviderLatencyTotal   time.Duration
	ProviderLatencyMaximum time.Duration
}

type ServiceMetrics struct {
	inFlight               atomic.Int64
	overloadRejections     atomic.Uint64
	timeouts               atomic.Uint64
	cancellations          atomic.Uint64
	providerLatencySamples atomic.Uint64
	providerLatencyTotalNS atomic.Int64
	providerLatencyMaxNS   atomic.Int64
}

func (m *ServiceMetrics) Snapshot() MetricsSnapshot {
	return MetricsSnapshot{
		InFlight:               m.inFlight.Load(),
		OverloadRejections:     m.overloadRejections.Load(),
		Timeouts:               m.timeouts.Load(),
		Cancellations:          m.cancellations.Load(),
		ProviderLatencySamples: m.providerLatencySamples.Load(),
		ProviderLatencyTotal:   time.Duration(m.providerLatencyTotalNS.Load()),
		ProviderLatencyMaximum: time.Duration(m.providerLatencyMaxNS.Load()),
	}
}

type providerLimiter struct {
	slots   chan struct{}
	metrics *ServiceMetrics
}

func newProviderLimiter(capacity int, metrics *ServiceMetrics) *providerLimiter {
	return &providerLimiter{slots: make(chan struct{}, capacity), metrics: metrics}
}

func (l *providerLimiter) tryAcquire() bool {
	select {
	case l.slots <- struct{}{}:
		l.metrics.inFlight.Add(1)
		return true
	default:
		return false
	}
}

func (l *providerLimiter) release() {
	<-l.slots
	l.metrics.inFlight.Add(-1)
}

type Service struct {
	provider        Model
	limiter         *providerLimiter
	metrics         *ServiceMetrics
	analysisTimeout time.Duration
	providerTimeout time.Duration
}

func NewService(provider Model) *Service {
	return newService(provider, ProviderConcurrencyLimit, AnalysisTimeout, ProviderTimeout)
}

func newService(provider Model, capacity int, analysisTimeout, providerTimeout time.Duration) *Service {
	metrics := &ServiceMetrics{}
	return &Service{
		provider:        provider,
		limiter:         newProviderLimiter(capacity, metrics),
		metrics:         metrics,
		analysisTimeout: analysisTimeout,
		providerTimeout: providerTimeout,
	}
}

func (s *Service) MetricsSnapshot() MetricsSnapshot { return s.metrics.Snapshot() }

var errProviderOverloaded = errors.New("provider concurrency limit reached")

func (s *Service) callProvider(parent context.Context, trusted Context) ([]byte, error) {
	if err := parent.Err(); err != nil {
		return nil, err
	}
	if !s.limiter.tryAcquire() {
		s.metrics.overloadRejections.Add(1)
		return nil, errProviderOverloaded
	}
	start := time.Now()
	defer func() {
		elapsed := time.Since(start)
		s.metrics.providerLatencySamples.Add(1)
		s.metrics.providerLatencyTotalNS.Add(int64(elapsed))
		for previous := s.metrics.providerLatencyMaxNS.Load(); int64(elapsed) > previous; previous = s.metrics.providerLatencyMaxNS.Load() {
			if s.metrics.providerLatencyMaxNS.CompareAndSwap(previous, int64(elapsed)) {
				break
			}
		}
	}()
	released := false
	defer func() {
		if !released {
			s.limiter.release()
		}
	}()

	providerCtx, cancel := context.WithTimeout(parent, s.providerTimeout)
	defer cancel()
	raw, callErr := s.provider.Explain(providerCtx, cloneContext(trusted))
	// Release immediately after Explain returns. If Explain panics, the deferred
	// release still runs during stack unwinding. No goroutine outlives this call.
	s.limiter.release()
	released = true
	if errors.Is(callErr, context.DeadlineExceeded) || errors.Is(providerCtx.Err(), context.DeadlineExceeded) {
		s.metrics.timeouts.Add(1)
		return nil, context.DeadlineExceeded
	}
	if errors.Is(callErr, context.Canceled) || errors.Is(providerCtx.Err(), context.Canceled) {
		s.metrics.cancellations.Add(1)
		return nil, context.Canceled
	}
	return raw, callErr
}

type ErrorCategory string

const (
	CategoryInvalidInput       ErrorCategory = "invalid_input"
	CategoryInvalidModelOutput ErrorCategory = "invalid_model_output"
	CategoryUpstreamFailure    ErrorCategory = "upstream_provider_failure"
	CategoryStaleContext       ErrorCategory = "stale_context"
	CategoryInternal           ErrorCategory = "internal_error"
	CategoryOverloaded         ErrorCategory = "provider_overloaded"
)

type ValidationError struct {
	Category ErrorCategory
	Field    string
	Cause    error
}

func (e *ValidationError) Error() string {
	if e.Cause != nil {
		return e.Category.String() + ": " + e.Cause.Error()
	}
	return e.Category.String()
}

func (c ErrorCategory) String() string { return string(c) }

func (e *ValidationError) Unwrap() error { return e.Cause }

func validationError(category ErrorCategory, field string, cause error) error {
	return &ValidationError{Category: category, Field: field, Cause: cause}
}

type metricSpec struct {
	valueType  string
	unit       string
	definition string
}

var metricSpecs = map[string]metricSpec{
	"total_income": {
		"MONEY", "VND",
		"All-time sum of active INCOME entries plus linked REVERSAL entries for INCOME originals, without an event-date filter; excludes TRANSFER, EXPENSE, REFUND, and ADJUSTMENT events. VND is stored at two decimal places and emitted at that precision without further rounding.",
	},
	"total_expense": {
		"MONEY", "VND",
		"All-time negated sum of active EXPENSE entries plus linked REFUND and REVERSAL entries for EXPENSE originals, without an event-date filter; excludes TRANSFER, INCOME, and ADJUSTMENT events. VND is stored at two decimal places and emitted at that precision without further rounding.",
	},
	"balance": {
		"MONEY", "VND",
		"Reported economic balance equal to total_income minus total_expense for the all-time period; it does not include ADJUSTMENT events. VND is stored at two decimal places and emitted at that precision without further rounding.",
	},
	"net_worth": {
		"MONEY", "VND",
		"Sum of all active canonical signed account movements, including adjustments and transfers, across all owner accounts; VND is stored at two decimal places and emitted at that precision without further rounding; not a market valuation.",
	},
	"account_count": {
		"COUNT", "accounts",
		"Exact integer count of the owner's financial accounts, including accounts with zero balance; no rounding.",
	},
	"expense_to_income_percent": {
		"PERCENTAGE", "percent",
		"total_expense divided by total_income multiplied by 100, rounded half-up to two decimal percentage points; null when total_income is zero.",
	},
}

var moneyPattern = regexp.MustCompile(`^-?[0-9]+\.[0-9]{2}$`)
var integerPattern = regexp.MustCompile(`^(0|[1-9][0-9]*)$`)
var snapshotPattern = regexp.MustCompile(`^snap_v2_[0-9a-f]{64}$`)
var fingerprintPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

// DeterministicModel is a local development fake, not an LLM. These claim codes
// are deterministic observations and do not require model inference.
type DeterministicModel struct{}

func (DeterministicModel) Explain(_ context.Context, c Context) ([]byte, error) {
	output := ModelOutput{
		OutputSchemaVersion: OutputSchemaVersion,
		SnapshotID:          c.SnapshotID,
		Findings: []ModelFinding{
			findingForMetric(c, "total_income"),
			findingForMetric(c, "total_expense"),
			findingForMetric(c, "balance"),
		},
	}
	output.Findings = compactFindings(output.Findings)
	return json.Marshal(output)
}

func findingForMetric(c Context, metric string) ModelFinding {
	var factID string
	for _, fact := range c.Facts {
		if fact.Metric == metric {
			factID = fact.ID
			break
		}
	}
	for _, item := range c.Evidence {
		if item.FactID == factID && factID != "" {
			return ModelFinding{
				Metric:     MetricKey(metric),
				FactID:     factID,
				EvidenceID: item.ID,
				ClaimCode:  ClaimValueReported,
			}
		}
	}
	return ModelFinding{}
}

func compactFindings(findings []ModelFinding) []ModelFinding {
	result := make([]ModelFinding, 0, len(findings))
	for _, finding := range findings {
		if finding.FactID != "" && finding.EvidenceID != "" {
			result = append(result, finding)
		}
	}
	return result
}

func stableID(prefix string, domain string, parts ...string) string {
	payload := domain
	for _, part := range parts {
		payload += "\x00" + part
	}
	digest := sha256.Sum256([]byte(payload))
	return prefix + "_" + hex.EncodeToString(digest[:])
}

func nullableValueToken(value *string) string {
	if value == nil {
		return "<null>"
	}
	return *value
}

func expectedFactID(snapshotID string, f Fact) string {
	return stableID(
		"fact_v2",
		"pfp-financial-fact-v2",
		snapshotID,
		f.Metric,
		f.ValueType,
		nullableValueToken(f.Value),
		f.Unit,
		f.Period,
		f.Availability,
	)
}

func expectedEvidenceID(snapshotID string, factID string, source string) string {
	return stableID("evidence_v2", "pfp-financial-evidence-v2", snapshotID, factID, source)
}

func Validate(c Context) error {
	if c.SchemaVersion != 2 || c.AnalysisType != "all_time_financial_summary" || c.Period != "all_time" || c.Currency != "VND" || c.DataStatus != "FRESH" {
		return errors.New("unsupported or non-fresh analysis context")
	}
	if c.Generation < 1 || !snapshotPattern.MatchString(c.SnapshotID) {
		return errors.New("invalid snapshot identifier")
	}
	if c.Reporting.Kind != "ALL_TIME" || c.Reporting.Start != nil || c.Reporting.End != nil || c.Reporting.Timezone != "UTC" {
		return errors.New("unsupported reporting period")
	}
	if !fingerprintPattern.MatchString(c.Freshness.CanonicalFingerprint) || c.Freshness.Status != "FRESH" || c.Freshness.Generation != c.Generation {
		return errors.New("invalid projection freshness metadata")
	}
	for _, timestamp := range []string{c.GeneratedAt, c.Freshness.CheckedAt, c.Freshness.ProjectionRebuiltAt} {
		if _, err := time.Parse(time.RFC3339Nano, timestamp); err != nil {
			return errors.New("invalid context timestamp")
		}
	}
	for _, value := range []string{c.Metrics.TotalIncome, c.Metrics.TotalExpense, c.Metrics.Balance, c.Metrics.NetWorth} {
		if !moneyPattern.MatchString(value) {
			return errors.New("missing/invalid financial metric")
		}
	}
	if c.Metrics.AccountCount < 0 {
		return errors.New("invalid account count")
	}
	if c.Metrics.ExpenseToIncomePercent != nil && !moneyPattern.MatchString(*c.Metrics.ExpenseToIncomePercent) {
		return errors.New("invalid expense-to-income percentage")
	}
	if c.Metrics.TotalIncome == "0.00" && c.Metrics.ExpenseToIncomePercent != nil {
		return errors.New("expense-to-income percentage must be null when income is zero")
	}
	if c.Metrics.TotalIncome != "0.00" && c.Metrics.ExpenseToIncomePercent == nil {
		return errors.New("expense-to-income percentage missing for non-zero income")
	}
	if len(c.Facts) != len(metricSpecs) || len(c.Evidence) != len(metricSpecs) {
		return errors.New("incomplete financial facts or evidence")
	}

	expectedValues := map[string]*string{
		"total_income":              stringPointer(c.Metrics.TotalIncome),
		"total_expense":             stringPointer(c.Metrics.TotalExpense),
		"balance":                   stringPointer(c.Metrics.Balance),
		"net_worth":                 stringPointer(c.Metrics.NetWorth),
		"account_count":             stringPointer(strconv.Itoa(c.Metrics.AccountCount)),
		"expense_to_income_percent": c.Metrics.ExpenseToIncomePercent,
	}
	seenFacts := make(map[string]Fact, len(c.Facts))
	for _, f := range c.Facts {
		spec, ok := metricSpecs[f.Metric]
		if !ok || seenFacts[f.Metric].Metric != "" {
			return errors.New("unknown or duplicate financial fact")
		}
		if f.ValueType != spec.valueType || f.Unit != spec.unit || f.Period != "ALL_TIME" || f.Definition != spec.definition {
			return errors.New("financial fact definition does not match contract")
		}
		want := expectedValues[f.Metric]
		if !sameNullableString(f.Value, want) {
			return errors.New("financial fact value does not match metrics")
		}
		wantAvailability := "AVAILABLE"
		if f.Metric == "expense_to_income_percent" && f.Value == nil {
			wantAvailability = "UNDEFINED_ZERO_DENOMINATOR"
		}
		if f.Availability != wantAvailability || f.ID != expectedFactID(c.SnapshotID, f) {
			return errors.New("invalid financial fact identity or availability")
		}
		if f.Value != nil {
			switch f.ValueType {
			case "MONEY", "PERCENTAGE":
				if !moneyPattern.MatchString(*f.Value) {
					return errors.New("invalid decimal financial fact")
				}
			case "COUNT":
				if !integerPattern.MatchString(*f.Value) {
					return errors.New("invalid count financial fact")
				}
			}
		}
		seenFacts[f.Metric] = f
	}

	seenEvidence := make(map[string]bool, len(c.Evidence))
	seenEvidenceFacts := make(map[string]bool, len(c.Evidence))
	for _, item := range c.Evidence {
		fact, ok := factByID(seenFacts, item.FactID)
		if !ok || item.SnapshotID != c.SnapshotID || item.Source != "financial_projection_state" || seenEvidence[item.ID] || seenEvidenceFacts[item.FactID] {
			return errors.New("malformed or unrelated evidence record")
		}
		if item.ID != expectedEvidenceID(c.SnapshotID, fact.ID, item.Source) {
			return errors.New("evidence identifier does not match snapshot fact")
		}
		seenEvidence[item.ID] = true
		seenEvidenceFacts[item.FactID] = true
	}
	return nil
}

func validateTrustedContext(c Context) error {
	if c.DataStatus != "FRESH" || c.Freshness.Status != "FRESH" || c.Freshness.Generation != c.Generation {
		return validationError(CategoryStaleContext, "context.freshness", errors.New("context is stale or freshness metadata is inconsistent"))
	}
	if err := Validate(c); err != nil {
		return validationError(CategoryInvalidInput, "context", err)
	}
	return nil
}

func ValidateModelOutput(raw []byte, trusted Context) (ModelOutput, error) {
	if err := validateTrustedContext(trusted); err != nil {
		return ModelOutput{}, err
	}
	if len(raw) > MaxModelOutputBytes {
		return ModelOutput{}, validationError(CategoryInvalidModelOutput, "model_output", errors.New("model output exceeds byte limit"))
	}

	var output ModelOutput
	if err := decodeStrictJSON(raw, &output); err != nil {
		return ModelOutput{}, validationError(CategoryInvalidModelOutput, "model_output", err)
	}
	if output.OutputSchemaVersion != OutputSchemaVersion {
		return ModelOutput{}, validationError(CategoryInvalidModelOutput, "output_schema_version", errors.New("unsupported model output version"))
	}
	if output.SnapshotID != trusted.SnapshotID {
		return ModelOutput{}, validationError(CategoryInvalidModelOutput, "snapshot_id", errors.New("snapshot does not match trusted context"))
	}
	if output.Findings == nil {
		return ModelOutput{}, validationError(CategoryInvalidModelOutput, "findings", errors.New("findings must be an array; it may be empty"))
	}
	if len(output.Findings) > MaxFindings {
		return ModelOutput{}, validationError(CategoryInvalidModelOutput, "findings", errors.New("too many findings"))
	}

	factsByID := make(map[string]Fact, len(trusted.Facts))
	for _, fact := range trusted.Facts {
		factsByID[fact.ID] = fact
	}
	evidenceByID := make(map[string]Evidence, len(trusted.Evidence))
	for _, evidence := range trusted.Evidence {
		evidenceByID[evidence.ID] = evidence
	}
	seen := make(map[string]bool, len(output.Findings))
	for i, finding := range output.Findings {
		field := fmt.Sprintf("findings[%d]", i)
		fact, ok := factsByID[finding.FactID]
		if !ok {
			return ModelOutput{}, validationError(CategoryInvalidModelOutput, field+".fact_id", errors.New("fact is not in trusted context"))
		}
		if string(finding.Metric) != fact.Metric {
			return ModelOutput{}, validationError(CategoryInvalidModelOutput, field+".metric", errors.New("metric does not match referenced fact"))
		}
		evidence, ok := evidenceByID[finding.EvidenceID]
		if !ok || evidence.SnapshotID != trusted.SnapshotID || evidence.FactID != fact.ID {
			return ModelOutput{}, validationError(CategoryInvalidModelOutput, field+".evidence_id", errors.New("evidence does not support this fact in the trusted snapshot"))
		}
		if !claimAllowed(finding.ClaimCode, fact, trusted) {
			return ModelOutput{}, validationError(CategoryInvalidModelOutput, field+".claim_code", errors.New("claim code is not supported by the referenced fact"))
		}
		key := string(finding.FactID) + "\x00" + string(finding.ClaimCode)
		if seen[key] {
			return ModelOutput{}, validationError(CategoryInvalidModelOutput, field, errors.New("duplicate finding"))
		}
		seen[key] = true
	}
	return output, nil
}

func claimAllowed(code ClaimCode, fact Fact, trusted Context) bool {
	switch code {
	case ClaimValueReported:
		return fact.Availability == "AVAILABLE" && fact.Value != nil
	case ClaimRatioUndefinedZeroIncome:
		return fact.Metric == "expense_to_income_percent" &&
			fact.Availability == "UNDEFINED_ZERO_DENOMINATOR" &&
			fact.Value == nil && trusted.Metrics.TotalIncome == "0.00"
	default:
		return false
	}
}

func decodeStrictJSON(raw []byte, target any) error {
	if err := rejectDuplicateKeysAndTrailingValues(raw); err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		if err == nil {
			return errors.New("trailing JSON value")
		}
		return err
	}
	return nil
}

func rejectDuplicateKeysAndTrailingValues(raw []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := scanJSONValue(decoder); err != nil {
		return err
	}
	if _, err := decoder.Token(); err != io.EOF {
		if err == nil {
			return errors.New("trailing JSON value")
		}
		return err
	}
	return nil
}

func scanJSONValue(decoder *json.Decoder) error {
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	delim, isDelim := token.(json.Delim)
	if !isDelim {
		return nil
	}
	switch delim {
	case '{':
		seen := make(map[string]bool)
		for decoder.More() {
			keyToken, err := decoder.Token()
			if err != nil {
				return err
			}
			key, ok := keyToken.(string)
			if !ok {
				return errors.New("object key is not a string")
			}
			if seen[key] {
				return fmt.Errorf("duplicate JSON key %q", key)
			}
			seen[key] = true
			if err := scanJSONValue(decoder); err != nil {
				return err
			}
		}
		end, err := decoder.Token()
		if err != nil || end != json.Delim('}') {
			return errors.New("unterminated JSON object")
		}
	case '[':
		for decoder.More() {
			if err := scanJSONValue(decoder); err != nil {
				return err
			}
		}
		end, err := decoder.Token()
		if err != nil || end != json.Delim(']') {
			return errors.New("unterminated JSON array")
		}
	default:
		return errors.New("unexpected JSON delimiter")
	}
	return nil
}

func stringPointer(value string) *string { return &value }

// cloneContext prevents a Model implementation from changing the trusted
// context used for output validation and response rendering.
func cloneContext(c Context) Context {
	cloneString := func(value *string) *string {
		if value == nil {
			return nil
		}
		copy := *value
		return &copy
	}
	c.Reporting.Start = cloneString(c.Reporting.Start)
	c.Reporting.End = cloneString(c.Reporting.End)
	c.Metrics.ExpenseToIncomePercent = cloneString(c.Metrics.ExpenseToIncomePercent)
	c.Facts = append([]Fact(nil), c.Facts...)
	for i := range c.Facts {
		c.Facts[i].Value = cloneString(c.Facts[i].Value)
	}
	c.Evidence = append([]Evidence(nil), c.Evidence...)
	c.Limitations = append([]string(nil), c.Limitations...)
	return c
}

func sameNullableString(a, b *string) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return *a == *b
}

func factByID(facts map[string]Fact, id string) (Fact, bool) {
	for _, f := range facts {
		if f.ID == id {
			return f, true
		}
	}
	return Fact{}, false
}

func RenderResult(c Context, output ModelOutput) Result {
	factsByID := make(map[string]Fact, len(c.Facts))
	for _, fact := range c.Facts {
		factsByID[fact.ID] = fact
	}
	findings := make([]Finding, 0, len(output.Findings))
	for _, modelFinding := range output.Findings {
		fact := factsByID[modelFinding.FactID]
		description := ""
		if modelFinding.ClaimCode == ClaimRatioUndefinedZeroIncome {
			description = "Tỷ lệ chi tiêu trên thu nhập không xác định vì tổng thu bằng 0."
		} else {
			description = reportedFactDescription(fact)
		}
		findings = append(findings, Finding{
			Metric:      fact.Metric,
			Description: description,
			Evidence:    []string{modelFinding.EvidenceID},
		})
	}
	return Result{
		Summary:     "Tóm tắt tài chính tích lũy dựa trên dữ liệu đã được xác nhận.",
		Findings:    findings,
		Limitations: append([]string(nil), c.Limitations...),
	}
}

func reportedFactDescription(fact Fact) string {
	value := ""
	if fact.Value != nil {
		value = *fact.Value
	}
	switch fact.Metric {
	case "total_income":
		return fmt.Sprintf("Tổng thu: %s %s", value, fact.Unit)
	case "total_expense":
		return fmt.Sprintf("Tổng chi: %s %s", value, fact.Unit)
	case "balance":
		return fmt.Sprintf("Số dư kinh tế: %s %s", value, fact.Unit)
	case "net_worth":
		return fmt.Sprintf("Giá trị tài sản ròng ghi nhận: %s %s", value, fact.Unit)
	case "account_count":
		return fmt.Sprintf("Số lượng tài khoản: %s", value)
	case "expense_to_income_percent":
		return fmt.Sprintf("Tỷ lệ chi tiêu trên thu nhập: %s%%", value)
	default:
		return ""
	}
}

func Handler(service *Service, internalKey string) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	mux.HandleFunc("POST /internal/analyze", func(w http.ResponseWriter, r *http.Request) {
		if internalKey == "" || subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Internal-Token")), []byte(internalKey)) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		// Phase 2B trusts Context provenance from FastAPI. This service validates
		// its internal consistency and binds findings to it; it does not verify a
		// FastAPI signature or independently query financial storage.
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, MaxIncomingRequestBytes))
		if err != nil {
			writeError(w, validationError(CategoryInvalidInput, "request", errors.New("request body is invalid or too large")))
			return
		}
		var req struct {
			Context Context `json:"context"`
		}
		if err := decodeStrictJSON(body, &req); err != nil {
			writeError(w, validationError(CategoryInvalidInput, "request", err))
			return
		}
		if err := validateTrustedContext(req.Context); err != nil {
			writeError(w, err)
			return
		}
		analysisCtx, cancel := context.WithTimeout(r.Context(), service.analysisTimeout)
		defer cancel()
		rawOutput, err := service.callProvider(analysisCtx, req.Context)
		if err != nil {
			if r.Context().Err() != nil {
				return
			}
			if errors.Is(err, errProviderOverloaded) {
				writeError(w, validationError(CategoryOverloaded, "provider", err))
				return
			}
			writeError(w, validationError(CategoryUpstreamFailure, "provider", err))
			return
		}
		output, err := ValidateModelOutput(rawOutput, req.Context)
		if err != nil {
			writeError(w, err)
			return
		}
		result := RenderResult(req.Context, output)
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		_ = json.NewEncoder(w).Encode(result)
	})
	return mux
}

func writeError(w http.ResponseWriter, err error) {
	var validation *ValidationError
	if !errors.As(err, &validation) {
		validation = &ValidationError{Category: CategoryInternal, Cause: err}
	}
	status := http.StatusInternalServerError
	switch validation.Category {
	case CategoryInvalidInput:
		status = http.StatusBadRequest
	case CategoryStaleContext:
		status = http.StatusConflict
	case CategoryInvalidModelOutput:
		status = http.StatusBadGateway
	case CategoryUpstreamFailure:
		status = http.StatusBadGateway
		if errors.Is(validation.Cause, context.DeadlineExceeded) {
			status = http.StatusGatewayTimeout
		}
	case CategoryOverloaded:
		status = http.StatusServiceUnavailable
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	if validation.Category == CategoryOverloaded {
		w.Header().Set("Retry-After", "1")
	}
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": string(validation.Category)})
}
