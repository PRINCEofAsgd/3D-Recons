// Argo 控制面使用合成 Workflow 和 SQL mock 验证身份、部分成功与重复回报。
package main

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/DATA-DOG/go-sqlmock"
)

func syntheticArgoJob() argoJob {
	return argoJob{ID: strings.Repeat("a", 32), Dataset: "synthetic", Candidate: "estimated",
		Snapshot: "s3://synthetic/phase4/manifests/snapshots/synthetic/snapshot1/manifest.json",
		Workflow: "sv-" + strings.Repeat("a", 32), Status: "running", Profile: "synthetic-small", Image: "store-vision-cloud:1.0.11"}
}

func TestArgoWorkflowIdentityParameters(t *testing.T) {
	j := syntheticArgoJob()
	data, err := workflowObject(j, "demo", "store-vision-pipeline")
	if err != nil {
		t.Fatal(err)
	}
	var value struct {
		Metadata struct {
			Name string `json:"name"`
		} `json:"metadata"`
		Spec struct {
			Arguments struct {
				Parameters []struct{ Name, Value string } `json:"parameters"`
			} `json:"arguments"`
		} `json:"spec"`
	}
	if err := json.Unmarshal(data, &value); err != nil {
		t.Fatal(err)
	}
	if value.Metadata.Name != j.Workflow {
		t.Fatal(value.Metadata.Name)
	}
	var workflow argoWorkflow
	if err := json.Unmarshal(data, &workflow); err != nil {
		t.Fatal(err)
	}
	if !workflowMatchesJob(workflow, j) {
		t.Fatal("workflow identity rejected")
	}
	got := map[string]string{}
	for _, p := range value.Spec.Arguments.Parameters {
		got[p.Name] = p.Value
	}
	for key, expected := range map[string]string{"dataset_id": j.Dataset, "job_id": j.ID, "run_id": j.ID,
		"attempt_id": "attempt1", "candidate": "estimated", "snapshot_uri": j.Snapshot,
		"image": "store-vision-cloud:1.0.11", "stitch_profile": "synthetic-small"} {
		if got[key] != expected {
			t.Fatalf("%s=%q", key, got[key])
		}
	}
	j.Candidate = "wrong"
	if workflowMatchesJob(workflow, j) {
		t.Fatal("wrong candidate workflow accepted")
	}
	if _, err := workflowObject(j, "demo", "store-vision-pipeline"); err == nil {
		t.Fatal("wrong candidate accepted")
	}
}

func TestArgoSubmitAndPartialArtifactQuery(t *testing.T) {
	t.Setenv("SV_ARGO_IMAGE", "store-vision-cloud:1.0.11")
	s, mock, db := mockServer(t)
	defer db.Close()
	bad := httptest.NewRecorder()
	s.routes().ServeHTTP(bad, request("POST", "/jobs", `{"dataset_id":"synthetic","backend":"argo","candidate":"wrong"}`))
	if bad.Code != 400 {
		t.Fatalf("wrong candidate status=%d", bad.Code)
	}
	uri := s.snapshotURI("synthetic", "snapshot1")
	mock.ExpectQuery("SELECT snapshot_uri FROM datasets").WithArgs("synthetic", "synthetic-owner").WillReturnRows(sqlmock.NewRows([]string{"snapshot_uri"}).AddRow(uri))
	mock.ExpectBegin()
	mock.ExpectExec("INSERT INTO jobs").WithArgs(sqlmock.AnyArg(), "synthetic", "synthetic-owner", "synthetic-key", argoDigest("synthetic", uri, "estimated", "synthetic-small", "store-vision-cloud:1.0.11"), uri, "argo", "estimated", "synthetic-small", "store-vision-cloud:1.0.11").WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectExec("INSERT INTO stages").WithArgs(sqlmock.AnyArg(), "verify_snapshot").WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectCommit()
	w := httptest.NewRecorder()
	s.routes().ServeHTTP(w, request("POST", "/jobs", `{"dataset_id":"synthetic","backend":"argo","candidate":"estimated","stitch_profile":"synthetic-small"}`))
	if w.Code != 202 {
		t.Fatalf("status=%d body=%s", w.Code, w.Body.String())
	}
	id := strings.Repeat("a", 32)
	mock.ExpectQuery("SELECT id,dataset_id,status,status_version,attempts").WithArgs(id, "synthetic-owner").WillReturnRows(sqlmock.NewRows([]string{"id", "dataset_id", "status", "status_version", "attempts", "failure_type", "input_manifest_uri", "output_manifest_uri"}).AddRow(id, "synthetic", "failed", 2, 1, "branch_failed", uri, nil))
	mock.ExpectQuery("SELECT name,attempt_id,manifest_uri FROM artifacts").WithArgs(id).WillReturnRows(sqlmock.NewRows([]string{"name", "attempt_id", "manifest_uri"}).AddRow("stitching", "attempt1", "s3://synthetic/phase2/stitching"))
	w = httptest.NewRecorder()
	s.routes().ServeHTTP(w, request("GET", "/jobs/"+id+"/artifacts", ""))
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"stitching"`) {
		t.Fatalf("status=%d body=%s", w.Code, w.Body.String())
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestArgoSummaryPartialBranchAndReplay(t *testing.T) {
	s, mock, db := mockServer(t)
	defer db.Close()
	j := syntheticArgoJob()
	artifact := "s3://synthetic/phase2/manifests/artifacts/synthetic/" + j.ID + "/attempt1/stitching/manifest.json"
	publication := "s3://synthetic/phase2/manifests/published/synthetic/" + j.ID + "/attempt1/manifest.json"
	summary := pipelineSummary{Dataset: j.Dataset, Run: j.ID, Attempt: "attempt1", Candidate: "estimated", Status: "failed",
		StitchProfile: j.Profile,
		SnapshotURI:   j.Snapshot, RunURI: "s3://synthetic/phase2/manifests/runs/synthetic/" + j.ID + "/attempt1/manifest.json",
		RunSHA: strings.Repeat("a", 64), PublicationURI: publication, Stages: map[string]branchSummary{"map25d": {Status: "failed", Reason: "Argo node: Failed"},
			"stitching": {Status: "succeeded"}}, Artifacts: map[string]string{"stitching": artifact}}
	encoded, _ := json.Marshal(summary)
	wf := argoWorkflow{}
	wf.Status.Nodes = map[string]argoNode{"summary": {DisplayName: "summarize-job-state", Phase: "Succeeded"}}
	node := wf.Status.Nodes["summary"]
	node.Outputs.Parameters = append(node.Outputs.Parameters, struct {
		Name  string `json:"name"`
		Value string `json:"value"`
	}{Name: "stage-result", Value: string(encoded)})
	wf.Status.Nodes["summary"] = node
	parsed, err := summaryFromWorkflow(wf, j, s.bucket, s.prefix)
	if err != nil || parsed.Status != "failed" || len(parsed.Artifacts) != 1 {
		t.Fatalf("summary=%+v err=%v", parsed, err)
	}
	mock.ExpectBegin()
	mock.ExpectQuery("SELECT status,status_version FROM jobs").WithArgs(j.ID).WillReturnRows(sqlmock.NewRows([]string{"status", "status_version"}).AddRow("running", 2))
	mock.ExpectExec("UPDATE jobs SET status=").WithArgs("failed", "branch_failed", publication, j.ID, uint64(2), "running").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec("INSERT INTO stages").WithArgs(j.ID, "map25d", "failed", nil, "branch_failed", "Argo node: Failed").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec("INSERT INTO stages").WithArgs(j.ID, "stitching", "succeeded", artifact, nil, nil).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec("INSERT INTO artifacts").WithArgs(j.ID, "stitching", artifact).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec("INSERT INTO stages").WithArgs(j.ID, "failed", publication, "branch_failed", "branch_failed").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectCommit()
	if err := s.settleArgo(context.Background(), j, parsed, ""); err != nil {
		t.Fatal(err)
	}
	// 同一 Workflow 事件重放时终态已锁定，不能重复插入 Artifact。
	mock.ExpectBegin()
	mock.ExpectQuery("SELECT status,status_version FROM jobs").WithArgs(j.ID).WillReturnRows(sqlmock.NewRows([]string{"status", "status_version"}).AddRow("failed", 3))
	mock.ExpectRollback()
	if err := s.settleArgo(context.Background(), j, parsed, ""); err != nil {
		t.Fatal(err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestArgoUpstreamFailureBlocksUnstartedStages(t *testing.T) {
	// verify 失败后 Argo 会把依赖节点标成 Omitted；对外不能留下 pending。
	s, mock, db := mockServer(t)
	defer db.Close()
	j := syntheticArgoJob()
	wf := argoWorkflow{}
	wf.Status.Nodes = map[string]argoNode{
		"calibrate": {DisplayName: "calibrate-and-publish-intermediate", Phase: "Omitted", Message: "omitted: depends condition not met"},
		"summary":   {DisplayName: "summarize-job-state", Phase: "Omitted", Message: "omitted: depends condition not met"},
	}
	if nodeStage("Omitted") != "blocked" || terminalSummaryFailure(wf) != "upstream_failed" {
		t.Fatal("unstarted Argo nodes must retain upstream failure")
	}
	mock.ExpectExec("INSERT INTO stages").WithArgs(j.ID, "calibrate", "blocked", nil, "upstream_failed", "omitted: depends condition not met").WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectExec("INSERT INTO stages").WithArgs(j.ID, "summarize", "blocked", nil, "upstream_failed", "omitted: depends condition not met").WillReturnResult(sqlmock.NewResult(1, 1))
	if err := s.syncNodes(context.Background(), j, wf); err != nil {
		t.Fatal(err)
	}
	mock.ExpectBegin()
	mock.ExpectQuery("SELECT status,status_version FROM jobs").WithArgs(j.ID).WillReturnRows(sqlmock.NewRows([]string{"status", "status_version"}).AddRow("running", 2))
	mock.ExpectExec("UPDATE jobs SET status=").WithArgs("failed", "upstream_failed", nil, j.ID, uint64(2), "running").WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec("INSERT INTO stages").WithArgs(j.ID, "map25d").WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectExec("INSERT INTO stages").WithArgs(j.ID, "stitching").WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectExec("INSERT INTO stages").WithArgs(j.ID, "blocked", nil, "upstream_failed", "upstream_failed").WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectCommit()
	if err := s.settleArgo(context.Background(), j, nil, terminalSummaryFailure(wf)); err != nil {
		t.Fatal(err)
	}
	if err := mock.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}
