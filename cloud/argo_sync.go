// Phase 4 Argo 提交与可重放同步器：Workflow 是执行记录，MySQL 是对外业务状态。
package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"os/exec"
	"strings"
	"time"
)

type argoJob struct {
	ID, Dataset, Snapshot, Candidate, Profile, Image, Workflow, Status string
	Version                                                            uint64
	ClaimedAt                                                          time.Time
}

type argoNode struct {
	DisplayName string `json:"displayName"`
	Phase       string `json:"phase"`
	Message     string `json:"message"`
	Outputs     struct {
		Parameters []struct {
			Name  string `json:"name"`
			Value string `json:"value"`
		} `json:"parameters"`
	} `json:"outputs"`
}

type argoWorkflow struct {
	Metadata struct {
		Name   string            `json:"name"`
		Labels map[string]string `json:"labels"`
	} `json:"metadata"`
	Spec struct {
		Arguments struct {
			Parameters []struct {
				Name  string `json:"name"`
				Value string `json:"value"`
			} `json:"parameters"`
		} `json:"arguments"`
	} `json:"spec"`
	Status struct {
		Phase string              `json:"phase"`
		Nodes map[string]argoNode `json:"nodes"`
	} `json:"status"`
}

// workflowMatchesJob 核对已存在 Workflow 的全部业务参数，避免同名资源污染 Job。
func workflowMatchesJob(wf argoWorkflow, j argoJob) bool {
	if wf.Metadata.Name != j.Workflow || wf.Metadata.Labels["store-vision-job"] != j.ID {
		return false
	}
	values := map[string]string{}
	for _, p := range wf.Spec.Arguments.Parameters {
		values[p.Name] = p.Value
	}
	for name, expected := range map[string]string{"dataset_id": j.Dataset, "job_id": j.ID, "run_id": j.ID,
		"attempt_id": "attempt1", "candidate": j.Candidate, "snapshot_uri": j.Snapshot,
		"image": j.Image, "stitch_profile": j.Profile} {
		if values[name] != expected {
			return false
		}
	}
	return true
}

type branchSummary struct {
	Status string `json:"status"`
	Reason string `json:"reason"`
}

type pipelineSummary struct {
	Dataset        string                   `json:"dataset_id"`
	Run            string                   `json:"run_id"`
	Attempt        string                   `json:"attempt_id"`
	Candidate      string                   `json:"candidate"`
	StitchProfile  string                   `json:"stitch_profile"`
	Status         string                   `json:"status"`
	SnapshotURI    string                   `json:"snapshot_uri"`
	PublicationURI string                   `json:"publication_uri"`
	RunURI         string                   `json:"run_manifest_uri"`
	RunSHA         string                   `json:"run_sha256"`
	Stages         map[string]branchSummary `json:"stages"`
	Artifacts      map[string]string        `json:"artifacts"`
}

func argoEnv(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

// kubectl 通过用户或服务账户的 kubeconfig 访问 Argo CRD，不在 API 中嵌入集群凭据。
func kubectl(ctx context.Context, input []byte, args ...string) ([]byte, error) {
	// 单次 API 操作限时，避免控制器断连时阻塞所有 Job 的同步轮询。
	callCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	cmd := exec.CommandContext(callCtx, argoEnv("SV_KUBECTL", "kubectl"), args...)
	cmd.Stdin = bytes.NewReader(input)
	output, err := cmd.CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("kubectl %s: %w: %s", strings.Join(args, " "), err, strings.TrimSpace(string(output)))
	}
	return output, nil
}

// workflowObject 固定 Job、run、candidate、snapshot 和参数版本；名字支持崩溃后重建。
func workflowObject(j argoJob, namespace, template string) ([]byte, error) {
	if !identity.MatchString(j.Dataset) || !identity.MatchString(j.Candidate) || j.Candidate != "estimated" || len(j.ID) != 32 {
		return nil, errors.New("invalid Argo job identity")
	}
	if j.Profile != "default" && j.Profile != "synthetic-small" {
		return nil, errors.New("invalid stitch profile")
	}
	if j.Image == "" {
		return nil, errors.New("missing image reference")
	}
	parameters := []map[string]string{}
	for _, item := range [][2]string{{"dataset_id", j.Dataset}, {"job_id", j.ID}, {"run_id", j.ID},
		{"attempt_id", "attempt1"}, {"candidate", j.Candidate}, {"snapshot_uri", j.Snapshot},
		{"image", j.Image}, {"stitch_profile", j.Profile}} {
		parameters = append(parameters, map[string]string{"name": item[0], "value": item[1]})
	}
	object := map[string]any{"apiVersion": "argoproj.io/v1alpha1", "kind": "Workflow",
		"metadata": map[string]any{"name": j.Workflow, "namespace": namespace,
			"labels": map[string]string{"store-vision-job": j.ID}},
		"spec": map[string]any{"workflowTemplateRef": map[string]string{"name": template},
			"arguments": map[string]any{"parameters": parameters}}}
	return json.Marshal(object)
}

// claimArgo 仅修改 Argo Job；Go worker 的租约查询不会领取它。
func (s *server) claimArgo(ctx context.Context) error {
	rows, err := s.db.QueryContext(ctx, `SELECT id,status_version FROM jobs WHERE backend='argo' AND status='pending' ORDER BY created_at LIMIT 20`)
	if err != nil {
		return err
	}
	defer rows.Close()
	type item struct {
		id      string
		version uint64
	}
	items := []item{}
	for rows.Next() {
		var row item
		if err := rows.Scan(&row.id, &row.version); err != nil {
			return err
		}
		items = append(items, row)
	}
	for _, row := range items {
		name := "sv-" + row.id
		_, err = s.db.ExecContext(ctx, `UPDATE jobs SET status='running',status_version=status_version+1,attempts=1,workflow_name=? WHERE id=? AND backend='argo' AND status='pending' AND status_version=?`, name, row.id, row.version)
		if err != nil {
			return err
		}
	}
	return nil
}

func (s *server) activeArgo(ctx context.Context) ([]argoJob, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id,dataset_id,input_manifest_uri,candidate,stitch_profile,image_ref,workflow_name,status,status_version,updated_at FROM jobs WHERE backend='argo' AND status IN ('running','cancel_requested') ORDER BY created_at LIMIT 100`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	jobs := []argoJob{}
	for rows.Next() {
		var j argoJob
		var name sql.NullString
		if err := rows.Scan(&j.ID, &j.Dataset, &j.Snapshot, &j.Candidate, &j.Profile, &j.Image, &name, &j.Status, &j.Version, &j.ClaimedAt); err != nil {
			return nil, err
		}
		j.Workflow = name.String
		jobs = append(jobs, j)
	}
	return jobs, rows.Err()
}

func workflowNode(wf argoWorkflow, display string) (argoNode, bool) {
	for _, node := range wf.Status.Nodes {
		if node.DisplayName == display {
			return node, true
		}
	}
	return argoNode{}, false
}

func nodeStage(phase string) string {
	switch phase {
	case "Succeeded":
		return "succeeded"
	case "Failed", "Error":
		return "failed"
	case "Running":
		return "running"
	case "Omitted", "Skipped":
		return "blocked"
	default:
		return "pending"
	}
}

// 汇总未启动时保留上游失败语义；只有汇总已运行但输出无效才属于 invalid_summary。
func terminalSummaryFailure(wf argoWorkflow) string {
	if node, ok := workflowNode(wf, "summarize-job-state"); ok && nodeStage(node.Phase) == "blocked" {
		return "upstream_failed"
	}
	return "invalid_summary"
}

// summaryFromWorkflow 仅解析 summarize Pod 写出的已发布清单身份，不信任任意节点输出。
func summaryFromWorkflow(wf argoWorkflow, j argoJob, bucket, prefix string) (*pipelineSummary, error) {
	// Argo 重试后结果可能落在成功的 Pod 子节点，优先读取带输出的节点。
	for _, node := range wf.Status.Nodes {
		if node.Phase != "Succeeded" || (node.DisplayName != "summarize-job-state" && !strings.HasPrefix(node.DisplayName, "summarize-job-state(")) {
			continue
		}
		for _, param := range node.Outputs.Parameters {
			if param.Name != "stage-result" {
				continue
			}
			var summary pipelineSummary
			if err := json.Unmarshal([]byte(param.Value), &summary); err != nil {
				return nil, err
			}
			expected := fmt.Sprintf("s3://%s/%s/manifests/published/%s/%s/attempt1/manifest.json", bucket, prefix, j.Dataset, j.ID)
			expectedRun := fmt.Sprintf("s3://%s/%s/manifests/runs/%s/%s/attempt1/manifest.json", bucket, prefix, j.Dataset, j.ID)
			if summary.Dataset != j.Dataset || summary.Run != j.ID || summary.Attempt != "attempt1" ||
				summary.Candidate != j.Candidate || summary.SnapshotURI != j.Snapshot || summary.PublicationURI != expected ||
				summary.StitchProfile != j.Profile || summary.RunURI != expectedRun || len(summary.RunSHA) != 64 ||
				(summary.Status != "succeeded" && summary.Status != "failed") {
				return nil, errors.New("summary identity mismatch")
			}
			for name, uri := range summary.Artifacts {
				if name != "map25d" && name != "stitching" {
					return nil, errors.New("invalid artifact name")
				}
				expectedArtifact := fmt.Sprintf("s3://%s/%s/manifests/artifacts/%s/%s/attempt1/%s/manifest.json", bucket, prefix, j.Dataset, j.ID, name)
				if uri != expectedArtifact || summary.Stages[name].Status != "succeeded" {
					return nil, errors.New("artifact identity mismatch")
				}
			}
			if summary.Status == "succeeded" && (summary.Stages["stitching"].Status != "succeeded" ||
				(summary.Stages["map25d"].Status != "succeeded" && summary.Stages["map25d"].Status != "blocked")) {
				return nil, errors.New("invalid success criterion")
			}
			if summary.Status == "failed" && summary.Stages["stitching"].Status == "succeeded" &&
				(summary.Stages["map25d"].Status == "succeeded" || summary.Stages["map25d"].Status == "blocked") {
				return nil, errors.New("invalid failure criterion")
			}
			return &summary, nil
		}
	}
	return nil, errors.New("summary output missing")
}

// syncNodes 只更新运行视图；终态由 settleArgo 在单个数据库事务内提交。
func (s *server) syncNodes(ctx context.Context, j argoJob, wf argoWorkflow) error {
	for _, item := range [][2]string{{"verify-snapshot", "verify_snapshot"}, {"calibrate-and-publish-intermediate", "calibrate"},
		{"map25d-branch", "map25d"}, {"ray-stitch-branch", "stitching"}, {"summarize-job-state", "summarize"}} {
		node, ok := workflowNode(wf, item[0])
		if !ok {
			continue
		}
		status := nodeStage(node.Phase)
		reason := ""
		if status == "failed" {
			reason = "workflow_node_failed"
		} else if status == "blocked" {
			reason = "upstream_failed"
		}
		uri := ""
		if item[1] == "verify_snapshot" {
			uri = j.Snapshot
		}
		if item[1] == "calibrate" && status == "succeeded" {
			for _, p := range node.Outputs.Parameters {
				if p.Name == "run-uri" {
					uri = p.Value
				}
			}
		}
		detail := node.Message
		if len([]rune(detail)) > 1024 {
			detail = string([]rune(detail)[:1024])
		}
		_, err := s.db.ExecContext(ctx, `INSERT INTO stages(job_id,name,status,attempt_id,manifest_uri,failure_type,failure_detail) VALUES(?,?,?,'attempt1',?,?,?) ON DUPLICATE KEY UPDATE status=VALUES(status),attempt_id=VALUES(attempt_id),manifest_uri=VALUES(manifest_uri),failure_type=VALUES(failure_type),failure_detail=VALUES(failure_detail)`,
			j.ID, item[1], status, nullable(uri), nullable(reason), nullable(detail))
		if err != nil {
			return err
		}
	}
	return nil
}

// settleArgo 条件锁定 Job；重复或乱序轮询不能覆盖已经提交的终态。
func (s *server) settleArgo(ctx context.Context, j argoJob, summary *pipelineSummary, failure string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var status string
	var version uint64
	if err := tx.QueryRowContext(ctx, `SELECT status,status_version FROM jobs WHERE id=? AND backend='argo' FOR UPDATE`, j.ID).Scan(&status, &version); err != nil {
		return err
	}
	if status != "running" && status != "cancel_requested" {
		return nil
	}
	final := "failed"
	if summary != nil && summary.Status == "succeeded" && status != "cancel_requested" {
		final = "succeeded"
	}
	if status == "cancel_requested" {
		failure = "cancelled"
	} else if final == "failed" && failure == "" {
		failure = "branch_failed"
	}
	var publication any
	if summary != nil {
		publication = summary.PublicationURI
	}
	changed, err := tx.ExecContext(ctx, `UPDATE jobs SET status=?,status_version=status_version+1,failure_type=?,output_manifest_uri=? WHERE id=? AND backend='argo' AND status_version=? AND status=?`,
		final, nullable(failure), publication, j.ID, version, status)
	if err != nil {
		return err
	}
	count, _ := changed.RowsAffected()
	if count != 1 {
		return errors.New("concurrent Argo state change")
	}
	if summary != nil {
		for _, name := range []string{"map25d", "stitching"} {
			branch := summary.Stages[name]
			if branch.Status != "succeeded" && branch.Status != "failed" && branch.Status != "blocked" {
				return errors.New("invalid branch status")
			}
			reason := ""
			if branch.Status == "blocked" {
				reason = "capability_blocked"
			}
			if branch.Status == "failed" {
				reason = "branch_failed"
			}
			detail := branch.Reason
			if len([]rune(detail)) > 1024 {
				detail = string([]rune(detail)[:1024])
			}
			_, err = tx.ExecContext(ctx, `INSERT INTO stages(job_id,name,status,attempt_id,manifest_uri,failure_type,failure_detail) VALUES(?,?,?,'attempt1',?,?,?) ON DUPLICATE KEY UPDATE failure_detail=IF(VALUES(status)='failed' AND failure_detail IS NOT NULL,failure_detail,VALUES(failure_detail)),status=VALUES(status),manifest_uri=VALUES(manifest_uri),failure_type=VALUES(failure_type)`,
				j.ID, name, branch.Status, nullable(summary.Artifacts[name]), nullable(reason), nullable(detail))
			if err != nil {
				return err
			}
		}
		for name, uri := range summary.Artifacts {
			_, err = tx.ExecContext(ctx, `INSERT INTO artifacts(job_id,name,attempt_id,manifest_uri) VALUES(?,?,'attempt1',?) ON DUPLICATE KEY UPDATE manifest_uri=VALUES(manifest_uri)`, j.ID, name, uri)
			if err != nil {
				return err
			}
		}
	} else {
		// 上游失败使分支从未启动时，明确记为 blocked，而不是留下 pending。
		for _, name := range []string{"map25d", "stitching"} {
			_, err = tx.ExecContext(ctx, `INSERT INTO stages(job_id,name,status,attempt_id,failure_type,failure_detail) VALUES(?,?,'blocked','attempt1','upstream_failed','上游阶段失败，业务分支未运行') ON DUPLICATE KEY UPDATE failure_detail=IF(status IN ('pending','running'),VALUES(failure_detail),failure_detail),failure_type=IF(status IN ('pending','running'),VALUES(failure_type),failure_type),status=IF(status IN ('pending','running'),VALUES(status),status)`, j.ID, name)
			if err != nil {
				return err
			}
		}
	}
	// 上游节点被跳过时汇总 Pod 并未执行，不能把未运行阶段标成算法失败。
	summarizeStatus := final
	if summary == nil && failure == "upstream_failed" {
		summarizeStatus = "blocked"
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO stages(job_id,name,status,attempt_id,manifest_uri,failure_type,failure_detail) VALUES(?,'summarize',?,'attempt1',?,?,?) ON DUPLICATE KEY UPDATE status=VALUES(status),manifest_uri=VALUES(manifest_uri),failure_type=VALUES(failure_type),failure_detail=VALUES(failure_detail)`,
		j.ID, summarizeStatus, publication, nullable(failure), nullable(failure))
	if err != nil {
		return err
	}
	return tx.Commit()
}

// argoSync 每五秒轮询；重启后从 MySQL running Job 和确定性 Workflow 名恢复。
func (s *server) argoSync(ctx context.Context) error {
	namespace := argoEnv("SV_ARGO_NAMESPACE", "default")
	template := argoEnv("SV_ARGO_TEMPLATE", "store-vision-pipeline")
	for {
		// 尚未提交到 Argo 的取消请求可直接结算，避免没有 workflow_name 而悬挂。
		if _, err := s.db.ExecContext(ctx, `UPDATE jobs SET status='failed',failure_type='cancelled',status_version=status_version+1 WHERE backend='argo' AND status='cancel_requested' AND workflow_name IS NULL`); err != nil {
			return err
		}
		if _, err := s.db.ExecContext(ctx, `UPDATE stages AS st JOIN jobs AS j ON j.id=st.job_id SET st.status='failed',st.failure_type='cancelled' WHERE j.backend='argo' AND j.status='failed' AND j.failure_type='cancelled' AND st.status='cancel_requested'`); err != nil {
			return err
		}
		if err := s.claimArgo(ctx); err != nil {
			return err
		}
		jobs, err := s.activeArgo(ctx)
		if err != nil {
			return err
		}
		for _, j := range jobs {
			if j.Workflow == "" {
				continue
			}
			// Workflow 创建、控制器恢复和 Pod 执行合计超过全局期限后收束状态。
			if time.Since(j.ClaimedAt) > 2*time.Hour+15*time.Minute {
				if err := s.settleArgo(ctx, j, nil, "workflow_timeout"); err != nil {
					log.Printf("job=%s Argo timeout settlement: %v", j.ID, err)
				}
				continue
			}
			output, err := kubectl(ctx, nil, "get", "workflows.argoproj.io", j.Workflow, "-n", namespace, "-o", "json", "--ignore-not-found")
			if err != nil {
				log.Printf("job=%s Argo query: %v", j.ID, err)
				continue
			}
			if len(bytes.TrimSpace(output)) == 0 {
				object, err := workflowObject(j, namespace, template)
				if err != nil {
					log.Printf("job=%s Argo object: %v", j.ID, err)
					continue
				}
				if _, err = kubectl(ctx, object, "create", "-f", "-"); err != nil && !strings.Contains(err.Error(), "AlreadyExists") {
					log.Printf("job=%s Argo create: %v", j.ID, err)
				}
				continue
			}
			var wf argoWorkflow
			if err := json.Unmarshal(output, &wf); err != nil {
				log.Printf("job=%s Argo decode: %v", j.ID, err)
				continue
			}
			if !workflowMatchesJob(wf, j) {
				log.Printf("job=%s Argo workflow identity mismatch", j.ID)
				continue
			}
			if err := s.syncNodes(ctx, j, wf); err != nil {
				log.Printf("job=%s stage sync: %v", j.ID, err)
				continue
			}
			if wf.Status.Phase != "Succeeded" && wf.Status.Phase != "Failed" && wf.Status.Phase != "Error" {
				continue
			}
			summary, summaryErr := summaryFromWorkflow(wf, j, s.bucket, s.prefix)
			failure := ""
			if summaryErr != nil {
				failure = terminalSummaryFailure(wf)
			}
			if err := s.settleArgo(ctx, j, summary, failure); err != nil {
				log.Printf("job=%s Argo settlement: %v", j.ID, err)
			}
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(5 * time.Second):
		}
	}
}
