// 控制面分支测试使用合成身份和 SQL mock，不把模拟结果当成真实 MySQL。
package main

import (
	"context"
	"database/sql"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	sqlmock "github.com/DATA-DOG/go-sqlmock"
	mysql "github.com/go-sql-driver/mysql"
)

func request(method, path, body string) *http.Request {
	r := httptest.NewRequest(method, path, strings.NewReader(body))
	r.Header.Set("X-Owner-ID", "synthetic-owner")
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("Idempotency-Key", "synthetic-key")
	return r
}
func mockServer(t *testing.T) (*server, sqlmock.Sqlmock, *sql.DB) {
	t.Helper()
	db, mock, err := sqlmock.New()
	if err != nil {
		t.Fatal(err)
	}
	return &server{db: db, bucket: "synthetic", prefix: "phase2"}, mock, db
}
func TestSubmitDuplicateAndConflict(t *testing.T) {
	s, m, db := mockServer(t)
	defer db.Close()
	uri := s.snapshotURI("dataset1", "snapshot1")
	// 已有唯一键：相同请求返回已有 Job；更换 dataset 后同键必须冲突。
	for _, tc := range []struct {
		dataset, uri string
		code         int
	}{{"dataset1", uri, 200}, {"dataset2", s.snapshotURI("dataset2", "snapshot1"), 409}} {
		m.ExpectQuery("SELECT snapshot_uri FROM datasets").WithArgs(tc.dataset, "synthetic-owner").WillReturnRows(sqlmock.NewRows([]string{"snapshot_uri"}).AddRow(tc.uri))
		m.ExpectBegin()
		m.ExpectExec("INSERT INTO jobs").WillReturnError(&mysql.MySQLError{Number: 1062, Message: "duplicate"})
		m.ExpectRollback()
		m.ExpectQuery("SELECT id,request_digest,status FROM jobs").WithArgs("synthetic-owner", "synthetic-key").WillReturnRows(sqlmock.NewRows([]string{"id", "request_digest", "status"}).AddRow("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", digest("dataset1", uri), "pending"))
		w := httptest.NewRecorder()
		s.routes().ServeHTTP(w, request("POST", "/jobs", `{"dataset_id":"`+tc.dataset+`"}`))
		if w.Code != tc.code {
			t.Fatalf("dataset=%s status=%d body=%s", tc.dataset, w.Code, w.Body.String())
		}
	}
	if err := m.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}
func TestSubmitAtomicStageAndValidation(t *testing.T) {
	s, m, db := mockServer(t)
	defer db.Close()
	bad := request("POST", "/jobs", `{"dataset_id":"../escape"}`)
	w := httptest.NewRecorder()
	s.routes().ServeHTTP(w, bad)
	if w.Code != 400 {
		t.Fatal(w.Code)
	}
	uri := s.snapshotURI("dataset1", "snapshot1")
	m.ExpectQuery("SELECT snapshot_uri FROM datasets").WithArgs("dataset1", "synthetic-owner").WillReturnRows(sqlmock.NewRows([]string{"snapshot_uri"}).AddRow(uri))
	m.ExpectBegin()
	m.ExpectExec("INSERT INTO jobs").WillReturnResult(sqlmock.NewResult(1, 1))
	m.ExpectExec("INSERT INTO stages").WillReturnResult(sqlmock.NewResult(1, 1))
	m.ExpectCommit()
	w = httptest.NewRecorder()
	s.routes().ServeHTTP(w, request("POST", "/jobs", `{"dataset_id":"dataset1"}`))
	if w.Code != 202 {
		t.Fatalf("status=%d body=%s", w.Code, w.Body.String())
	}
	if err := m.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}
func TestStatusAfterServerRestartAndCancelCAS(t *testing.T) {
	s, m, db := mockServer(t)
	defer db.Close()
	id := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	rows := func() *sqlmock.Rows {
		return sqlmock.NewRows([]string{"id", "dataset_id", "status", "status_version", "attempts", "failure_type", "input_manifest_uri", "output_manifest_uri"}).AddRow(id, "dataset1", "running", 2, 1, nil, "s3://synthetic/phase2/input", nil)
	}
	m.ExpectQuery("SELECT id,dataset_id,status,status_version,attempts").WithArgs(id, "synthetic-owner").WillReturnRows(rows())
	w := httptest.NewRecorder()
	s.routes().ServeHTTP(w, request("GET", "/jobs/"+id, ""))
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"running"`) {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	restarted := &server{db: db, bucket: s.bucket, prefix: s.prefix}
	m.ExpectQuery("SELECT id,dataset_id,status,status_version,attempts").WithArgs(id, "synthetic-owner").WillReturnRows(rows())
	m.ExpectExec("UPDATE jobs SET status='cancel_requested'").WithArgs(id, "synthetic-owner", uint64(2)).WillReturnResult(sqlmock.NewResult(0, 1))
	m.ExpectExec("UPDATE stages SET status='cancel_requested'").WithArgs(id).WillReturnResult(sqlmock.NewResult(0, 1))
	w = httptest.NewRecorder()
	restarted.routes().ServeHTTP(w, request("POST", "/jobs/"+id+"/cancel", ""))
	if w.Code != 202 {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if err := m.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}

func TestExpiredAttemptClaimAfterWorkerRestart(t *testing.T) {
	s, m, db := mockServer(t)
	defer db.Close()
	id := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	m.ExpectQuery("SELECT id,owner_id,status_version FROM jobs").WillReturnRows(sqlmock.NewRows([]string{"id", "owner_id", "status_version"}).AddRow(id, "synthetic-owner", 4))
	m.ExpectExec("UPDATE jobs SET status='running'").WithArgs(id, uint64(4)).WillReturnResult(sqlmock.NewResult(0, 1))
	m.ExpectQuery("SELECT id,dataset_id,status,status_version,attempts").WithArgs(id, "synthetic-owner").WillReturnRows(sqlmock.NewRows([]string{"id", "dataset_id", "status", "status_version", "attempts", "failure_type", "input_manifest_uri", "output_manifest_uri"}).AddRow(id, "dataset1", "running", 5, 2, nil, "s3://synthetic/phase2/input", nil))
	m.ExpectExec("UPDATE stages SET status='running'").WithArgs("attempt2", id).WillReturnResult(sqlmock.NewResult(0, 1))
	restarted := &server{db: s.db, bucket: s.bucket, prefix: s.prefix}
	got, attempt, ok, err := restarted.claim(context.Background())
	if err != nil || !ok || got.ID != id || attempt != 2 {
		t.Fatalf("job=%+v attempt=%d ok=%v err=%v", got, attempt, ok, err)
	}
	if err := m.ExpectationsWereMet(); err != nil {
		t.Fatal(err)
	}
}
