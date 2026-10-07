// Phase 2 本地 Go 控制面：HTTP 只改元数据，算法由独立 worker 调用 Python。
package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	mysql "github.com/go-sql-driver/mysql"
)

var identity = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`)
var idemKey = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`)

type server struct {
	db             *sql.DB
	bucket, prefix string
}
type job struct {
	ID                string  `json:"id"`
	DatasetID         string  `json:"dataset_id"`
	Status            string  `json:"status"`
	StatusVersion     uint64  `json:"status_version"`
	Attempts          uint64  `json:"attempts"`
	FailureType       *string `json:"failure_type"`
	InputManifestURI  string  `json:"input_manifest_uri"`
	OutputManifestURI *string `json:"output_manifest_uri"`
}

// decode 限制请求大小并拒绝未知字段，避免身份和枚举被悄然忽略。
func decode(w http.ResponseWriter, r *http.Request, dst any) bool {
	if r.Header.Get("Content-Type") != "application/json" {
		http.Error(w, "content type must be application/json", 415)
		return false
	}
	d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	d.DisallowUnknownFields()
	if err := d.Decode(dst); err != nil {
		http.Error(w, "invalid JSON", 400)
		return false
	}
	var extra any
	if errors.Is(d.Decode(&extra), io.EOF) {
		return true
	}
	http.Error(w, "one JSON object required", 400)
	return false
}
func respond(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

// owner 只做本地资源归属字段的格式校验，不承担认证职责。
func owner(w http.ResponseWriter, r *http.Request) string {
	value := r.Header.Get("X-Owner-ID")
	if !identity.MatchString(value) {
		http.Error(w, "invalid X-Owner-ID", 400)
		return ""
	}
	return value
}

// newID 使用随机字节生成互不依赖的 Job 身份。
func newID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

// digest 将提交内容固定为可比较的幂等请求摘要。
func digest(datasetID, uri string) string {
	sum := sha256.Sum256([]byte(datasetID + "\n" + uri + "\nfull"))
	return hex.EncodeToString(sum[:])
}

// snapshotURI 限制注册请求只能引用当前桶和前缀中的快照。
func (s *server) snapshotURI(datasetID, snapshotID string) string {
	return fmt.Sprintf("s3://%s/%s/manifests/snapshots/%s/%s/manifest.json", s.bucket, s.prefix, datasetID, snapshotID)
}

// datasets 注册元数据；输入快照由 Python 命令先行上传和验证。
func (s *server) datasets(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", 405)
		return
	}
	who := owner(w, r)
	if who == "" {
		return
	}
	var req struct {
		ID          string `json:"id"`
		SnapshotID  string `json:"snapshot_id"`
		SnapshotURI string `json:"snapshot_uri"`
	}
	if !decode(w, r, &req) {
		return
	}
	if !identity.MatchString(req.ID) || !identity.MatchString(req.SnapshotID) || len(req.SnapshotURI) > 512 || req.SnapshotURI != s.snapshotURI(req.ID, req.SnapshotID) {
		http.Error(w, "invalid dataset or snapshot URI", 400)
		return
	}
	_, err := s.db.ExecContext(r.Context(), `INSERT INTO datasets(id,owner_id,snapshot_uri) VALUES(?,?,?)`, req.ID, who, req.SnapshotURI)
	if err != nil {
		var dbErr *mysql.MySQLError
		if errors.As(err, &dbErr) && dbErr.Number == 1062 {
			http.Error(w, "dataset already exists", 409)
		} else {
			http.Error(w, "database unavailable", 503)
		}
		return
	}
	respond(w, 201, map[string]string{"id": req.ID, "snapshot_uri": req.SnapshotURI})
}

// submit 用数据库唯一约束判定重复提交，并在一个事务中创建 stage。
func (s *server) submit(w http.ResponseWriter, r *http.Request) {
	who := owner(w, r)
	if who == "" {
		return
	}
	key := r.Header.Get("Idempotency-Key")
	if !idemKey.MatchString(key) {
		http.Error(w, "invalid Idempotency-Key", 400)
		return
	}
	var req struct {
		DatasetID string `json:"dataset_id"`
	}
	if !decode(w, r, &req) {
		return
	}
	if !identity.MatchString(req.DatasetID) {
		http.Error(w, "invalid dataset_id", 400)
		return
	}
	var uri string
	err := s.db.QueryRowContext(r.Context(), `SELECT snapshot_uri FROM datasets WHERE id=? AND owner_id=?`, req.DatasetID, who).Scan(&uri)
	if errors.Is(err, sql.ErrNoRows) {
		http.Error(w, "dataset not found", 404)
		return
	}
	if err != nil {
		http.Error(w, "database unavailable", 503)
		return
	}
	hash := digest(req.DatasetID, uri)
	id := newID()
	tx, err := s.db.BeginTx(r.Context(), nil)
	if err != nil {
		http.Error(w, "database unavailable", 503)
		return
	}
	defer tx.Rollback()
	_, err = tx.ExecContext(r.Context(), `INSERT INTO jobs(id,dataset_id,owner_id,idempotency_key,request_digest,input_manifest_uri) VALUES(?,?,?,?,?,?)`, id, req.DatasetID, who, key, hash, uri)
	if err == nil {
		_, err = tx.ExecContext(r.Context(), `INSERT INTO stages(job_id,name,status) VALUES(?,'pipeline','pending')`, id)
		if err != nil {
			http.Error(w, "stage creation failed", 503)
			return
		}
		if err = tx.Commit(); err != nil {
			http.Error(w, "database unavailable", 503)
			return
		}
		respond(w, 202, map[string]string{"id": id, "status": "pending"})
		return
	}
	_ = tx.Rollback()
	var dbErr *mysql.MySQLError
	if !errors.As(err, &dbErr) || dbErr.Number != 1062 {
		http.Error(w, "database unavailable", 503)
		return
	}
	var previousID, previousHash, status string
	lookup := s.db.QueryRowContext(r.Context(), `SELECT id,request_digest,status FROM jobs WHERE owner_id=? AND idempotency_key=?`, who, key).Scan(&previousID, &previousHash, &status)
	if lookup != nil {
		http.Error(w, "database unavailable", 503)
		return
	}
	if previousHash != hash {
		http.Error(w, "idempotency key conflicts with another request", 409)
		return
	}
	respond(w, 200, map[string]string{"id": previousID, "status": status})
}

// findJob 按 owner 过滤，阻止跨资源归属查询。
func (s *server) findJob(ctx context.Context, id, who string) (job, error) {
	var j job
	err := s.db.QueryRowContext(ctx, `SELECT id,dataset_id,status,status_version,attempts,failure_type,input_manifest_uri,output_manifest_uri FROM jobs WHERE id=? AND owner_id=?`, id, who).Scan(&j.ID, &j.DatasetID, &j.Status, &j.StatusVersion, &j.Attempts, &j.FailureType, &j.InputManifestURI, &j.OutputManifestURI)
	return j, err
}

// jobs 暴露查询、取消和已提交 Artifact；未成功时不泄露结果 URI。
func (s *server) jobs(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/jobs" && r.Method == http.MethodPost {
		s.submit(w, r)
		return
	}
	who := owner(w, r)
	if who == "" {
		return
	}
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) < 2 || parts[0] != "jobs" || !regexp.MustCompile(`^[0-9a-f]{32}$`).MatchString(parts[1]) {
		http.Error(w, "invalid job ID", 400)
		return
	}
	j, err := s.findJob(r.Context(), parts[1], who)
	if errors.Is(err, sql.ErrNoRows) {
		http.Error(w, "job not found", 404)
		return
	}
	if err != nil {
		http.Error(w, "database unavailable", 503)
		return
	}
	if len(parts) == 2 && r.Method == http.MethodGet {
		respond(w, 200, j)
		return
	}
	if len(parts) == 3 && parts[2] == "cancel" && r.Method == http.MethodPost {
		if j.Status == "cancel_requested" {
			respond(w, 200, j)
			return
		}
		if j.Status != "pending" && j.Status != "running" {
			http.Error(w, "job already terminal", 409)
			return
		}
		result, err := s.db.ExecContext(r.Context(), `UPDATE jobs SET status='cancel_requested',status_version=status_version+1 WHERE id=? AND owner_id=? AND status_version=? AND status IN ('pending','running')`, j.ID, who, j.StatusVersion)
		if err != nil {
			http.Error(w, "database unavailable", 503)
			return
		}
		n, _ := result.RowsAffected()
		if n != 1 {
			http.Error(w, "concurrent state change", 409)
			return
		}
		_, _ = s.db.ExecContext(r.Context(), `UPDATE stages SET status='cancel_requested' WHERE job_id=? AND status IN ('pending','running')`, j.ID)
		j.Status = "cancel_requested"
		j.StatusVersion++
		respond(w, 202, j)
		return
	}
	if len(parts) == 3 && parts[2] == "artifacts" && r.Method == http.MethodGet {
		if j.Status != "succeeded" {
			respond(w, 202, map[string]any{"status": j.Status, "artifacts": []any{}})
			return
		}
		rows, err := s.db.QueryContext(r.Context(), `SELECT name,attempt_id,manifest_uri FROM artifacts WHERE job_id=? ORDER BY name`, j.ID)
		if err != nil {
			http.Error(w, "database unavailable", 503)
			return
		}
		defer rows.Close()
		items := []map[string]string{}
		for rows.Next() {
			var name, attempt, uri string
			if rows.Scan(&name, &attempt, &uri) != nil {
				http.Error(w, "database unavailable", 503)
				return
			}
			items = append(items, map[string]string{"name": name, "attempt_id": attempt, "manifest_uri": uri})
		}
		respond(w, 200, map[string]any{"publication_uri": j.OutputManifestURI, "artifacts": items})
		return
	}
	http.Error(w, "not found", 404)
}

// routes 把控制面保持为单个本地 HTTP 服务。
func (s *server) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/datasets", s.datasets)
	mux.HandleFunc("/jobs", s.jobs)
	mux.HandleFunc("/jobs/", s.jobs)
	return mux
}

// claim 用版本 CAS 领取任务；暂态故障的 pending Job 到重试时间后才能被领取。
func (s *server) claim(ctx context.Context) (job, uint64, bool, error) {
	var id, who string
	var version uint64
	err := s.db.QueryRowContext(ctx, `SELECT id,owner_id,status_version FROM jobs WHERE (status='pending' AND (lease_until IS NULL OR lease_until<NOW(6))) OR (status='running' AND lease_until<NOW(6)) ORDER BY created_at LIMIT 1`).Scan(&id, &who, &version)
	if errors.Is(err, sql.ErrNoRows) {
		return job{}, 0, false, nil
	}
	if err != nil {
		return job{}, 0, false, err
	}
	result, err := s.db.ExecContext(ctx, `UPDATE jobs SET status='running',status_version=status_version+1,attempts=attempts+1,lease_until=DATE_ADD(NOW(6),INTERVAL 90 SECOND),failure_type=NULL WHERE id=? AND status_version=? AND ((status='pending' AND (lease_until IS NULL OR lease_until<NOW(6))) OR (status='running' AND lease_until<NOW(6))) AND attempts<3`, id, version)
	if err != nil {
		return job{}, 0, false, err
	}
	n, _ := result.RowsAffected()
	if n == 0 {
		return job{}, 0, false, nil
	}
	j, err := s.findJob(ctx, id, who)
	if err != nil {
		return job{}, 0, false, err
	}
	attempt := j.Attempts
	_, _ = s.db.ExecContext(ctx, `UPDATE stages SET status='running',attempt_id=?,failure_type=NULL WHERE job_id=? AND name='pipeline'`, fmt.Sprintf("attempt%d", attempt), id)
	return j, attempt, true, nil
}

// settleAbandoned 将未启动的取消请求及超过重试上限的过期租约收束为终态。
func (s *server) settleAbandoned(ctx context.Context) error {
	_, err := s.db.ExecContext(ctx, `UPDATE jobs SET status='failed',failure_type='cancelled',status_version=status_version+1,lease_until=NULL WHERE status='cancel_requested' AND (attempts=0 OR lease_until<NOW(6))`)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx, `UPDATE jobs SET status='failed',failure_type='lease_expired',status_version=status_version+1,lease_until=NULL WHERE status='running' AND attempts>=3 AND lease_until<NOW(6)`)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx, `UPDATE stages AS st JOIN jobs AS j ON j.id=st.job_id SET st.status='failed',st.failure_type=j.failure_type WHERE j.status='failed' AND j.failure_type IN ('cancelled','lease_expired') AND st.status IN ('pending','running','cancel_requested')`)
	return err
}

type result struct {
	PublicationURI string            `json:"publication_uri"`
	RunManifestURI string            `json:"run_manifest_uri"`
	Artifacts      map[string]string `json:"artifacts"`
}

// finish 在事务内条件提交结果；取消和重领都不能被旧执行器覆盖。
func (s *server) finish(ctx context.Context, j job, attempt uint64, out result, runErr error) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var state string
	var version uint64
	err = tx.QueryRowContext(ctx, `SELECT status,status_version FROM jobs WHERE id=? AND attempts=? FOR UPDATE`, j.ID, attempt).Scan(&state, &version)
	if err != nil {
		return err
	}
	if state != "running" && state != "cancel_requested" {
		return nil
	}
	final, kind := "succeeded", ""
	if state == "cancel_requested" {
		final, kind = "failed", "cancelled"
	} else if runErr != nil {
		var exitErr *exec.ExitError
		if errors.As(runErr, &exitErr) && exitErr.ExitCode() == 75 {
			kind = "storage_transient"
			if attempt < 3 {
				final = "pending"
			} else {
				final = "failed"
			}
		} else {
			final, kind = "failed", "execution"
		}
	}
	if final == "succeeded" && (out.PublicationURI == "" || len(out.Artifacts) != 2) {
		final, kind = "failed", "invalid_output"
	}
	var published any
	if final == "succeeded" {
		published = out.PublicationURI
	}
	// 暂态存储故障给服务恢复留出窗口；租约列在 pending 时表示下次可领取时间。
	changed, err := tx.ExecContext(ctx, `UPDATE jobs SET status=?,status_version=status_version+1,failure_type=?,output_manifest_uri=?,lease_until=CASE WHEN ?='pending' THEN DATE_ADD(NOW(6),INTERVAL 15 SECOND) ELSE NULL END WHERE id=? AND attempts=? AND status_version=? AND status=?`, final, nullable(kind), published, final, j.ID, attempt, version, state)
	if err != nil {
		return err
	}
	n, _ := changed.RowsAffected()
	if n != 1 {
		return errors.New("concurrent status change")
	}
	attemptID := fmt.Sprintf("attempt%d", attempt)
	_, err = tx.ExecContext(ctx, `UPDATE stages SET status=?,attempt_id=?,manifest_uri=?,failure_type=? WHERE job_id=? AND name='pipeline'`, final, attemptID, nullable(out.RunManifestURI), nullable(kind), j.ID)
	if err != nil {
		return err
	}
	if final == "succeeded" {
		for name, uri := range out.Artifacts {
			if name != "map25d" && name != "stitching" {
				return errors.New("invalid artifact name")
			}
			_, err = tx.ExecContext(ctx, `INSERT INTO artifacts(job_id,name,attempt_id,manifest_uri) VALUES(?,?,?,?)`, j.ID, name, attemptID, uri)
			if err != nil {
				return err
			}
		}
	}
	return tx.Commit()
}
func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// worker 领取有租约的 Job，并让 Python 子进程独立运行原有算法。
func (s *server) worker(ctx context.Context, python, scratchRoot, report string, failAfterUpload bool) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}
		if err := s.settleAbandoned(ctx); err != nil {
			return err
		}
		j, attempt, ok, err := s.claim(ctx)
		if err != nil {
			return err
		}
		if !ok {
			time.Sleep(2 * time.Second)
			continue
		}
		attemptID := fmt.Sprintf("attempt%d", attempt)
		log.Printf("job=%s attempt=%s status=running", j.ID, attemptID)
		scratch := filepath.Join(scratchRoot, j.ID, attemptID)
		if err = os.MkdirAll(filepath.Dir(scratch), 0700); err != nil {
			return err
		}
		args := []string{"-m", "store_vision.cloud_job_runner", "execute", "--snapshot-uri", j.InputManifestURI, "--run-id", j.ID, "--attempt-id", attemptID, "--scratch", scratch}
		if report != "" {
			args = append(args, "--report-json", report)
		}
		command := exec.CommandContext(ctx, python, args...)
		stop := make(chan struct{})
		go func() {
			ticker := time.NewTicker(20 * time.Second)
			defer ticker.Stop()
			for {
				select {
				case <-stop:
					return
				case <-ticker.C:
					_, _ = s.db.ExecContext(ctx, `UPDATE jobs SET lease_until=DATE_ADD(NOW(6),INTERVAL 90 SECOND) WHERE id=? AND attempts=? AND status IN ('running','cancel_requested')`, j.ID, attempt)
				}
			}
		}()
		output, runErr := command.CombinedOutput()
		close(stop)
		var out result
		if runErr == nil {
			lines := strings.Split(strings.TrimSpace(string(output)), "\n")
			runErr = json.Unmarshal([]byte(lines[len(lines)-1]), &out)
		}
		if runErr != nil {
			message := strings.TrimSpace(string(output))
			if len(message) > 400 {
				message = message[len(message)-400:]
			}
			log.Printf("job=%s attempt=%s execution failed: %v detail=%q", j.ID, attemptID, runErr, message)
		}
		if failAfterUpload && runErr == nil && out.PublicationURI != "" {
			// 合成故障实验：对象已发布，故意在 MySQL 终态提交前退出。
			log.Printf("job=%s attempt=%s injected failure after upload before database commit", j.ID, attemptID)
			return errors.New("injected after-upload failure")
		}
		if err = s.finish(ctx, j, attempt, out, runErr); err != nil {
			log.Printf("job=%s metadata commit failed: %v", j.ID, err)
		} else {
			var state string
			if queryErr := s.db.QueryRowContext(ctx, `SELECT status FROM jobs WHERE id=?`, j.ID).Scan(&state); queryErr == nil {
				log.Printf("job=%s attempt=%s status=%s", j.ID, attemptID, state)
			}
		}
		_ = os.RemoveAll(scratch)
	}
}
func main() {
	mode := flag.String("mode", "api", "api or worker")
	address := flag.String("listen", "127.0.0.1:8080", "HTTP listen address")
	python := flag.String("python", "python3", "Python with store_vision installed")
	scratch := flag.String("scratch", "../cloud-local/scratch", "worker scratch root")
	report := flag.String("synthetic-report", "", "explicit synthetic report for local acceptance only")
	failAfterUpload := flag.Bool("test-fail-after-upload", false, "inject one local failure after object publication")
	flag.Parse()
	dsn := os.Getenv("SV_MYSQL_DSN")
	if dsn == "" {
		log.Fatal("SV_MYSQL_DSN is required")
	}
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()
	db.SetMaxOpenConns(8)
	db.SetConnMaxLifetime(4 * time.Minute)
	if err = db.Ping(); err != nil {
		log.Fatal(err)
	}
	s := &server{db: db, bucket: os.Getenv("SV_S3_BUCKET"), prefix: os.Getenv("SV_S3_PREFIX")}
	if s.bucket == "" || s.prefix == "" {
		log.Fatal("SV_S3_BUCKET and SV_S3_PREFIX are required")
	}
	if *mode == "worker" {
		log.Fatal(s.worker(context.Background(), *python, *scratch, *report, *failAfterUpload))
	}
	if *mode != "api" {
		log.Fatal("invalid mode")
	}
	log.Printf("Phase 2 API listening on %s", *address)
	log.Fatal(http.ListenAndServe(*address, s.routes()))
}
