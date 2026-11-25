package analysis

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func sample() Context {
	income := "100.00"
	expense := "40.00"
	balance := "60.00"
	netWorth := "60.00"
	ratio := "40.00"
	c := Context{
		SchemaVersion: 2,
		AnalysisType:  "all_time_financial_summary",
		Currency:      "VND",
		Period:        "all_time",
		DataStatus:    "FRESH",
		Generation:    1,
		SnapshotID:    stableID("snap_v2", "pfp-analysis-snapshot-v2", "sample"),
		Reporting:     ReportingPeriod{Kind: "ALL_TIME", Timezone: "UTC"},
		Metrics: Metrics{
			TotalIncome: income, TotalExpense: expense, Balance: balance,
			NetWorth: netWorth, AccountCount: 1,
			ExpenseToIncomePercent: &ratio,
		},
		Freshness: Freshness{
			Status: "FRESH", Generation: 1,
			CanonicalFingerprint: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			ProjectionRebuiltAt:  "2026-01-02T00:00:00+00:00",
			CheckedAt:            "2026-01-03T00:00:00+00:00",
		},
		GeneratedAt: "2026-01-03T00:00:00+00:00",
		Limitations: []string{"all-time data"},
	}

	values := map[string]*string{
		"total_income": &income, "total_expense": &expense, "balance": &balance,
		"net_worth": &netWorth, "account_count": stringPointer("1"),
		"expense_to_income_percent": &ratio,
	}
	for metric, spec := range metricSpecs {
		fact := Fact{
			Metric: metric, ValueType: spec.valueType, Value: values[metric],
			Unit: spec.unit, Period: "ALL_TIME", Availability: "AVAILABLE",
			Definition: spec.definition,
		}
		fact.ID = expectedFactID(c.SnapshotID, fact)
		c.Facts = append(c.Facts, fact)
	}
	for _, fact := range c.Facts {
		source := "financial_projection_state"
		c.Evidence = append(c.Evidence, Evidence{
			ID:         expectedEvidenceID(c.SnapshotID, fact.ID, source),
			SnapshotID: c.SnapshotID, FactID: fact.ID, Source: source,
		})
	}
	return c
}

func outputFor(t *testing.T, c Context, metric string, claim ClaimCode) ModelOutput {
	t.Helper()
	for _, fact := range c.Facts {
		if fact.Metric != metric {
			continue
		}
		for _, evidence := range c.Evidence {
			if evidence.FactID == fact.ID {
				return ModelOutput{
					OutputSchemaVersion: OutputSchemaVersion,
					SnapshotID:          c.SnapshotID,
					Findings: []ModelFinding{{
						Metric: MetricKey(metric), FactID: fact.ID,
						EvidenceID: evidence.ID, ClaimCode: claim,
					}},
				}
			}
		}
	}
	t.Fatalf("no fact/evidence for metric %q", metric)
	return ModelOutput{}
}

func rawOutput(t *testing.T, output ModelOutput) []byte {
	t.Helper()
	data, err := json.Marshal(output)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func categoryOf(err error) ErrorCategory {
	var validation *ValidationError
	if errors.As(err, &validation) {
		return validation.Category
	}
	return ""
}

func TestValidOutputAndEmptyFindingsAccepted(t *testing.T) {
	c := sample()
	valid := rawOutput(t, outputFor(t, c, "total_income", ClaimValueReported))
	if _, err := ValidateModelOutput(valid, c); err != nil {
		t.Fatalf("valid output rejected: %v", err)
	}

	empty := rawOutput(t, ModelOutput{
		OutputSchemaVersion: OutputSchemaVersion,
		SnapshotID:          c.SnapshotID,
		Findings:            []ModelFinding{},
	})
	decoded, err := ValidateModelOutput(empty, c)
	if err != nil {
		t.Fatalf("empty findings rejected: %v", err)
	}
	if decoded.Findings == nil || len(decoded.Findings) != 0 {
		t.Fatalf("expected an explicit empty findings array, got %#v", decoded.Findings)
	}
}

func TestOutputRejectsVersionSnapshotFactEvidenceAndMetricMismatches(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*ModelOutput, Context)
	}{
		{"unsupported version", func(o *ModelOutput, _ Context) { o.OutputSchemaVersion = 2 }},
		{"snapshot mismatch", func(o *ModelOutput, _ Context) { o.SnapshotID = stableID("snap_v2", "other") }},
		{"unknown fact", func(o *ModelOutput, _ Context) { o.Findings[0].FactID = "fact_v2_unknown" }},
		{"unknown evidence", func(o *ModelOutput, _ Context) { o.Findings[0].EvidenceID = "evidence_v2_unknown" }},
		{"metric does not match fact", func(o *ModelOutput, _ Context) { o.Findings[0].Metric = "total_expense" }},
		{"evidence belongs to another fact", func(o *ModelOutput, c Context) {
			for _, evidence := range c.Evidence {
				if evidence.FactID != o.Findings[0].FactID {
					o.Findings[0].EvidenceID = evidence.ID
					return
				}
			}
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			c := sample()
			output := outputFor(t, c, "total_income", ClaimValueReported)
			test.mutate(&output, c)
			if _, err := ValidateModelOutput(rawOutput(t, output), c); categoryOf(err) != CategoryInvalidModelOutput {
				t.Fatalf("expected invalid model output, got %v", err)
			}
		})
	}
}

func TestContextIdentifiersAreDeterministicAndSnapshotBound(t *testing.T) {
	c := sample()
	fact := c.Facts[0]
	if got, want := expectedFactID(c.SnapshotID, fact), fact.ID; got != want {
		t.Fatalf("fact id is not deterministic: got %q want %q", got, want)
	}
	evidence := c.Evidence[0]
	if got, want := expectedEvidenceID(c.SnapshotID, evidence.FactID, evidence.Source), evidence.ID; got != want {
		t.Fatalf("evidence id is not deterministic: got %q want %q", got, want)
	}
	otherSnapshot := stableID("snap_v2", "other-snapshot")
	if expectedEvidenceID(otherSnapshot, evidence.FactID, evidence.Source) == evidence.ID {
		t.Fatal("evidence id was reusable across snapshots")
	}
}

func TestOutputRejectsUnknownFieldsDuplicateKeysAndTrailingJSON(t *testing.T) {
	c := sample()
	valid := string(rawOutput(t, outputFor(t, c, "total_income", ClaimValueReported)))
	tests := []struct {
		name string
		raw  string
	}{
		{"unknown field", strings.Replace(valid, `"claim_code":"VALUE_REPORTED"`, `"claim_code":"VALUE_REPORTED","amount":"12.00"`, 1)},
		{"duplicate top-level key", strings.Replace(valid, `"output_schema_version":1`, `"output_schema_version":1,"output_schema_version":1`, 1)},
		{"duplicate finding key", strings.Replace(valid, `"metric":"total_income"`, `"metric":"total_income","metric":"total_income"`, 1)},
		{"trailing JSON", valid + ` {}`},
		{"malformed JSON", `{"output_schema_version":`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, err := ValidateModelOutput([]byte(test.raw), c); categoryOf(err) != CategoryInvalidModelOutput {
				t.Fatalf("expected invalid model output, got %v", err)
			}
		})
	}
}

func TestOutputRejectsOversizeBeforeDecoding(t *testing.T) {
	c := sample()
	_, err := ValidateModelOutput(bytes.Repeat([]byte("x"), MaxModelOutputBytes+1), c)
	if categoryOf(err) != CategoryInvalidModelOutput {
		t.Fatalf("expected output size rejection, got %v", err)
	}
}

func TestClaimCodesMustMatchFactAvailability(t *testing.T) {
	c := sample()
	wrongCode := outputFor(t, c, "total_income", ClaimRatioUndefinedZeroIncome)
	if _, err := ValidateModelOutput(rawOutput(t, wrongCode), c); categoryOf(err) != CategoryInvalidModelOutput {
		t.Fatalf("unsupported claim accepted: %v", err)
	}

	zero := "0.00"
	c.Metrics.TotalIncome = zero
	c.Metrics.ExpenseToIncomePercent = nil
	for i := range c.Facts {
		fact := &c.Facts[i]
		switch fact.Metric {
		case "total_income":
			fact.Value = stringPointer(zero)
			fact.ID = expectedFactID(c.SnapshotID, *fact)
		case "expense_to_income_percent":
			fact.Value = nil
			fact.Availability = "UNDEFINED_ZERO_DENOMINATOR"
			fact.ID = expectedFactID(c.SnapshotID, *fact)
		}
	}
	// Build a self-consistent zero-income fixture from the updated facts.
	c.Evidence = c.Evidence[:0]
	for _, fact := range c.Facts {
		c.Evidence = append(c.Evidence, Evidence{
			ID:         expectedEvidenceID(c.SnapshotID, fact.ID, "financial_projection_state"),
			SnapshotID: c.SnapshotID, FactID: fact.ID, Source: "financial_projection_state",
		})
	}
	output := outputFor(t, c, "expense_to_income_percent", ClaimRatioUndefinedZeroIncome)
	if _, err := ValidateModelOutput(rawOutput(t, output), c); err != nil {
		t.Fatalf("valid zero-income claim rejected: %v", err)
	}

	badAvailable := outputFor(t, sample(), "total_income", ClaimRatioUndefinedZeroIncome)
	if _, err := ValidateModelOutput(rawOutput(t, badAvailable), sample()); categoryOf(err) != CategoryInvalidModelOutput {
		t.Fatalf("zero-denominator claim for an available fact accepted: %v", err)
	}
}

func TestDuplicateFindingKeyRejectedAndEmptyIsNotForced(t *testing.T) {
	c := sample()
	finding := outputFor(t, c, "total_income", ClaimValueReported).Findings[0]
	output := ModelOutput{OutputSchemaVersion: OutputSchemaVersion, SnapshotID: c.SnapshotID, Findings: []ModelFinding{finding, finding}}
	if _, err := ValidateModelOutput(rawOutput(t, output), c); categoryOf(err) != CategoryInvalidModelOutput {
		t.Fatalf("duplicate finding accepted: %v", err)
	}
	output.Findings = []ModelFinding{}
	decoded, err := ValidateModelOutput(rawOutput(t, output), c)
	if err != nil || len(decoded.Findings) != 0 {
		t.Fatalf("empty finding result should be accepted: output=%#v err=%v", decoded, err)
	}
}

type rawModel struct {
	output []byte
	err    error
}

func (m rawModel) Explain(context.Context, Context) ([]byte, error) { return m.output, m.err }

type mutatingModel struct{}

func (mutatingModel) Explain(_ context.Context, c Context) ([]byte, error) {
	c.SnapshotID = stableID("snap_v2", "mutated-context")
	return json.Marshal(ModelOutput{
		OutputSchemaVersion: OutputSchemaVersion,
		SnapshotID:          c.SnapshotID,
		Findings:            []ModelFinding{},
	})
}

func requestBody(t *testing.T, c Context) []byte {
	t.Helper()
	body, err := json.Marshal(map[string]Context{"context": c})
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func performRequest(h http.Handler, body []byte) *httptest.ResponseRecorder {
	req := httptest.NewRequest("POST", "/internal/analyze", bytes.NewReader(body))
	req.Header.Set("X-Internal-Token", "test-token")
	recorder := httptest.NewRecorder()
	h.ServeHTTP(recorder, req)
	return recorder
}

func testHandler(model Model) http.Handler {
	return Handler(NewService(model), "test-token")
}

func TestHandlerMapsOutputDeterministicallyAndPreservesHTTPResult(t *testing.T) {
	c := sample()
	output := ModelOutput{
		OutputSchemaVersion: OutputSchemaVersion,
		SnapshotID:          c.SnapshotID,
		Findings:            []ModelFinding{},
	}
	h := Handler(NewService(rawModel{output: rawOutput(t, output)}), "test-token")
	recorder := performRequest(h, requestBody(t, c))
	if recorder.Code != http.StatusOK {
		t.Fatalf("unexpected status %d: %s", recorder.Code, recorder.Body.String())
	}
	var got map[string]json.RawMessage
	if err := json.Unmarshal(recorder.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 || got["summary"] == nil || got["findings"] == nil || got["limitations"] == nil {
		t.Fatalf("HTTP Result contract changed: %s", recorder.Body.String())
	}
	var result Result
	if err := json.Unmarshal(recorder.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result.Findings == nil || len(result.Findings) != 0 {
		t.Fatalf("expected empty findings, got %#v", result.Findings)
	}

	reported := outputFor(t, c, "total_income", ClaimValueReported)
	recorder = performRequest(testHandler(rawModel{output: rawOutput(t, reported)}), requestBody(t, c))
	if recorder.Code != http.StatusOK {
		t.Fatalf("valid value claim failed: %s", recorder.Body.String())
	}
	if !strings.Contains(recorder.Body.String(), "Tổng thu: 100.00 VND") {
		t.Fatalf("trusted context value was not rendered: %s", recorder.Body.String())
	}
}

func TestModelCannotMutateTrustedContextUsedForValidation(t *testing.T) {
	c := sample()
	recorder := performRequest(testHandler(mutatingModel{}), requestBody(t, c))
	if recorder.Code != http.StatusBadGateway || !strings.Contains(recorder.Body.String(), string(CategoryInvalidModelOutput)) {
		t.Fatalf("output bound to model-mutated context was accepted: %d %s", recorder.Code, recorder.Body.String())
	}
}

func TestHandlerErrorCategoriesAndSanitizedMessages(t *testing.T) {
	c := sample()
	valid := requestBody(t, c)
	tests := []struct {
		name   string
		model  Model
		body   []byte
		status int
		code   string
	}{
		{"invalid input", rawModel{}, []byte(`{"context":`), 400, string(CategoryInvalidInput)},
		{"stale context", rawModel{}, requestBody(t, staleContext(c)), 409, string(CategoryStaleContext)},
		{"invalid model output", rawModel{output: []byte(`{}`)}, valid, 502, string(CategoryInvalidModelOutput)},
		{"provider failure", rawModel{err: errors.New("private provider detail")}, valid, 502, string(CategoryUpstreamFailure)},
		{"provider timeout", rawModel{err: context.DeadlineExceeded}, valid, 504, string(CategoryUpstreamFailure)},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			recorder := performRequest(testHandler(test.model), test.body)
			if recorder.Code != test.status || !strings.Contains(recorder.Body.String(), test.code) {
				t.Fatalf("got %d %s", recorder.Code, recorder.Body.String())
			}
			if strings.Contains(recorder.Body.String(), "private provider detail") {
				t.Fatal("internal/provider error details leaked")
			}
		})
	}

	tooLarge := append(append([]byte(nil), valid...), bytes.Repeat([]byte(" "), MaxIncomingRequestBytes)...)
	recorder := performRequest(testHandler(rawModel{}), tooLarge)
	if recorder.Code != 400 || !strings.Contains(recorder.Body.String(), string(CategoryInvalidInput)) {
		t.Fatalf("incoming request size limit not enforced: %d %s", recorder.Code, recorder.Body.String())
	}
}

func staleContext(c Context) Context {
	c.DataStatus = "STALE"
	c.Freshness.Status = "STALE"
	return c
}

func TestIncomingRequestRejectsDuplicateUnknownAndTrailingJSON(t *testing.T) {
	c := sample()
	valid := string(requestBody(t, c))
	tests := []string{
		strings.Replace(valid, `"context":`, `"context":{},"context":`, 1),
		strings.Replace(valid, `"context":`, `"unexpected":true,"context":`, 1),
		valid + ` {}`,
	}
	for _, body := range tests {
		recorder := performRequest(testHandler(rawModel{}), []byte(body))
		if recorder.Code != http.StatusBadRequest {
			t.Fatalf("invalid request accepted: %d %s", recorder.Code, recorder.Body.String())
		}
	}
}

func TestDeterministicModelProducesValidatedCodesWithoutLLM(t *testing.T) {
	c := sample()
	raw, err := (DeterministicModel{}).Explain(context.Background(), c)
	if err != nil {
		t.Fatal(err)
	}
	output, err := ValidateModelOutput(raw, c)
	if err != nil {
		t.Fatal(err)
	}
	if len(output.Findings) != 3 {
		t.Fatalf("expected three deterministic findings, got %d", len(output.Findings))
	}
	for _, finding := range output.Findings {
		if finding.ClaimCode != ClaimValueReported {
			t.Fatalf("unexpected claim code %q", finding.ClaimCode)
		}
	}
}

func TestSchemaDocumentIsValidJSONAndVersioned(t *testing.T) {
	data, err := os.ReadFile("model_output.schema.json")
	if err != nil {
		t.Fatal(err)
	}
	var schema map[string]any
	if err := json.Unmarshal(data, &schema); err != nil {
		t.Fatalf("invalid schema JSON: %v", err)
	}
	properties := schema["properties"].(map[string]any)
	version := properties["output_schema_version"].(map[string]any)
	if version["const"] != float64(OutputSchemaVersion) {
		t.Fatalf("schema version not synchronized: %#v", version["const"])
	}
	findings := properties["findings"].(map[string]any)
	if findings["maxItems"] != float64(MaxFindings) {
		t.Fatalf("schema maxItems not synchronized: %#v", findings["maxItems"])
	}
}

type canceledModel struct{}

func (canceledModel) Explain(ctx context.Context, _ Context) ([]byte, error) {
	return nil, ctx.Err()
}

func TestCanceledProviderRequestDoesNotProduceSuccess(t *testing.T) {
	c := sample()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	body := requestBody(t, c)
	req := httptest.NewRequest("POST", "/internal/analyze", bytes.NewReader(body)).WithContext(ctx)
	req.Header.Set("X-Internal-Token", "test-token")
	recorder := httptest.NewRecorder()
	testHandler(canceledModel{}).ServeHTTP(recorder, req)
	if recorder.Body.Len() != 0 {
		t.Fatalf("canceled request received a response body: %d %s", recorder.Code, recorder.Body.String())
	}
}

func TestWriteErrorUnknownErrorIsInternalAndSanitized(t *testing.T) {
	recorder := httptest.NewRecorder()
	writeError(recorder, fmt.Errorf("sensitive internal detail"))
	if recorder.Code != http.StatusInternalServerError || strings.Contains(recorder.Body.String(), "sensitive") {
		t.Fatalf("internal error was not sanitized: %d %s", recorder.Code, recorder.Body.String())
	}
}

type modelFunc func(context.Context, Context) ([]byte, error)

func (f modelFunc) Explain(ctx context.Context, c Context) ([]byte, error) { return f(ctx, c) }

func emptyModelOutput(c Context) []byte {
	data, _ := json.Marshal(ModelOutput{
		OutputSchemaVersion: OutputSchemaVersion,
		SnapshotID:          c.SnapshotID,
		Findings:            []ModelFinding{},
	})
	return data
}

type blockingModel struct {
	started chan<- struct{}
	release <-chan struct{}
	calls   atomic.Int64
}

func (m *blockingModel) Explain(ctx context.Context, c Context) ([]byte, error) {
	m.calls.Add(1)
	m.started <- struct{}{}
	select {
	case <-m.release:
		return emptyModelOutput(c), nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func TestProviderLimiterAdmitsTenAndRejectsEleventhPromptly(t *testing.T) {
	started := make(chan struct{}, ProviderConcurrencyLimit)
	release := make(chan struct{})
	provider := &blockingModel{started: started, release: release}
	service := NewService(provider)
	handler := Handler(service, "test-token")
	body := requestBody(t, sample())
	var wg sync.WaitGroup
	for range ProviderConcurrencyLimit {
		wg.Add(1)
		go func() {
			defer wg.Done()
			recorder := performRequest(handler, body)
			if recorder.Code != http.StatusOK {
				t.Errorf("admitted request got %d: %s", recorder.Code, recorder.Body.String())
			}
		}()
	}
	for range ProviderConcurrencyLimit {
		select {
		case <-started:
		case <-time.After(time.Second):
			close(release)
			t.Fatal("provider did not receive ten concurrent calls")
		}
	}
	if provider.calls.Load() != ProviderConcurrencyLimit || service.MetricsSnapshot().InFlight != ProviderConcurrencyLimit {
		t.Fatalf("expected exactly ten active provider calls, calls=%d metrics=%+v", provider.calls.Load(), service.MetricsSnapshot())
	}
	startedAt := time.Now()
	eleventh := performRequest(handler, body)
	if elapsed := time.Since(startedAt); elapsed > 100*time.Millisecond {
		t.Fatalf("overload rejection was not prompt: %s", elapsed)
	}
	if eleventh.Code != http.StatusServiceUnavailable || !strings.Contains(eleventh.Body.String(), string(CategoryOverloaded)) {
		t.Fatalf("11th request was not rejected with sanitized 503: %d %s", eleventh.Code, eleventh.Body.String())
	}
	if provider.calls.Load() != ProviderConcurrencyLimit {
		t.Fatalf("overloaded request reached provider: calls=%d", provider.calls.Load())
	}
	close(release)
	wg.Wait()
	snapshot := service.MetricsSnapshot()
	if snapshot.InFlight != 0 || snapshot.OverloadRejections != 1 || snapshot.ProviderLatencySamples != ProviderConcurrencyLimit {
		t.Fatalf("unexpected metrics after calls: %+v", snapshot)
	}
}

func TestProductionServiceUsesApprovedLimits(t *testing.T) {
	service := NewService(DeterministicModel{})
	if cap(service.limiter.slots) != ProviderConcurrencyLimit || service.analysisTimeout != 8*time.Second || service.providerTimeout != 7*time.Second {
		t.Fatalf("unexpected production limits: slots=%d analysis=%s provider=%s", cap(service.limiter.slots), service.analysisTimeout, service.providerTimeout)
	}
}

func TestProviderSlotReleasedAfterSuccessErrorAndPanic(t *testing.T) {
	tests := []struct {
		name       string
		firstErr   error
		firstPanic bool
	}{
		{name: "success"},
		{name: "provider error", firstErr: errors.New("provider failure")},
		{name: "panic unwinding", firstPanic: true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var calls atomic.Int64
			c := sample()
			service := newService(modelFunc(func(_ context.Context, input Context) ([]byte, error) {
				if calls.Add(1) == 1 {
					if test.firstPanic {
						panic("provider panic")
					}
					return emptyModelOutput(input), test.firstErr
				}
				return emptyModelOutput(input), nil
			}), 1, time.Second, time.Second)
			handler := Handler(service, "test-token")
			if test.firstPanic {
				func() {
					defer func() { _ = recover() }()
					performRequest(handler, requestBody(t, c))
				}()
			} else {
				performRequest(handler, requestBody(t, c))
			}
			if got := service.MetricsSnapshot().InFlight; got != 0 {
				t.Fatalf("slot not released after first call: %d", got)
			}
			second := performRequest(handler, requestBody(t, c))
			if second.Code != http.StatusOK {
				t.Fatalf("slot was not reusable after %s: %d %s", test.name, second.Code, second.Body.String())
			}
		})
	}
}

func TestProviderCancellationPropagatesAndReleasesSlot(t *testing.T) {
	started := make(chan struct{})
	canceled := make(chan struct{})
	service := newService(modelFunc(func(ctx context.Context, _ Context) ([]byte, error) {
		close(started)
		<-ctx.Done()
		close(canceled)
		return nil, ctx.Err()
	}), 1, time.Second, time.Second)
	requestContext, cancel := context.WithCancel(context.Background())
	req := httptest.NewRequest("POST", "/internal/analyze", bytes.NewReader(requestBody(t, sample()))).WithContext(requestContext)
	req.Header.Set("X-Internal-Token", "test-token")
	recorder := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		defer close(done)
		Handler(service, "test-token").ServeHTTP(recorder, req)
	}()
	<-started
	cancel()
	select {
	case <-canceled:
	case <-time.After(time.Second):
		t.Fatal("provider did not receive request cancellation")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("handler did not finish after cancellation")
	}
	snapshot := service.MetricsSnapshot()
	if snapshot.InFlight != 0 || snapshot.Cancellations != 1 {
		t.Fatalf("unexpected cancellation metrics: %+v", snapshot)
	}
}

func TestProviderTimeoutRetainsSlotUntilExplainReturns(t *testing.T) {
	started := make(chan struct{})
	timedOut := make(chan struct{})
	release := make(chan struct{})
	var calls atomic.Int64
	c := sample()
	service := newService(modelFunc(func(ctx context.Context, input Context) ([]byte, error) {
		if calls.Add(1) == 1 {
			close(started)
			// Ignore cancellation after observing the deadline to model a provider
			// that cannot be stopped by context cancellation.
			<-ctx.Done()
			close(timedOut)
			<-release
		}
		return emptyModelOutput(input), nil
	}), 1, 200*time.Millisecond, 30*time.Millisecond)
	recorder := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		defer close(done)
		Handler(service, "test-token").ServeHTTP(recorder, authorizedRequest(t, c))
	}()
	<-started
	select {
	case <-timedOut:
	case <-time.After(time.Second):
		t.Fatal("provider deadline did not expire")
	}
	if snapshot := service.MetricsSnapshot(); snapshot.InFlight != 1 {
		t.Fatalf("slot released while provider was still running: %+v", snapshot)
	}
	close(release)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("handler did not finish after provider returned")
	}
	if recorder.Code != http.StatusGatewayTimeout {
		t.Fatalf("timed out provider result was accepted: %d %s", recorder.Code, recorder.Body.String())
	}
	snapshot := service.MetricsSnapshot()
	if snapshot.InFlight != 0 || snapshot.Timeouts != 1 || snapshot.ProviderLatencySamples != 1 {
		t.Fatalf("unexpected timeout metrics: %+v", snapshot)
	}
	if got := performRequest(Handler(service, "test-token"), requestBody(t, c)); got.Code != http.StatusOK {
		t.Fatalf("slot not available after Explain returned: %d %s", got.Code, got.Body.String())
	}
}

func authorizedRequest(t *testing.T, c Context) *http.Request {
	t.Helper()
	req := httptest.NewRequest("POST", "/internal/analyze", bytes.NewReader(requestBody(t, c)))
	req.Header.Set("X-Internal-Token", "test-token")
	return req
}
