// 显式配置本地测试库后，检验真实 MySQL 唯一约束下的并发提交。
package main

import (
	"database/sql"
	"encoding/json"
	"net/http/httptest"
	"os"
	"sync"
	"testing"

	_ "github.com/go-sql-driver/mysql"
)

// 专用测试库中的真实唯一约束实验；未配置 DSN 时明确跳过。
func TestConcurrentSubmitMySQL(t *testing.T) {
	dsn := os.Getenv("SV_TEST_MYSQL_DSN")
	if dsn == "" {
		t.Skip("SV_TEST_MYSQL_DSN not set; real MySQL concurrency unverified")
	}
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err = db.Ping(); err != nil {
		t.Fatal(err)
	}
	id := "d" + newID()
	who := "o" + newID()
	key := "k" + newID()
	s := &server{db: db, bucket: "synthetic", prefix: "phase2"}
	uri := s.snapshotURI(id, "snapshot1")
	if _, err = db.Exec(`INSERT INTO datasets(id,owner_id,snapshot_uri) VALUES(?,?,?)`, id, who, uri); err != nil {
		t.Fatal(err)
	}
	defer func() {
		db.Exec(`DELETE FROM stages WHERE job_id IN (SELECT id FROM jobs WHERE owner_id=? AND idempotency_key=?)`, who, key)
		db.Exec(`DELETE FROM jobs WHERE owner_id=? AND idempotency_key=?`, who, key)
		db.Exec(`DELETE FROM datasets WHERE id=?`, id)
	}()
	const clients = 12
	var wg sync.WaitGroup
	ids := make(chan string, clients)
	codes := make(chan int, clients)
	for i := 0; i < clients; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r := request("POST", "/jobs", `{"dataset_id":"`+id+`"}`)
			r.Header.Set("X-Owner-ID", who)
			r.Header.Set("Idempotency-Key", key)
			w := httptest.NewRecorder()
			s.routes().ServeHTTP(w, r)
			codes <- w.Code
			var payload struct {
				ID string `json:"id"`
			}
			_ = json.Unmarshal(w.Body.Bytes(), &payload)
			ids <- payload.ID
		}()
	}
	wg.Wait()
	close(ids)
	close(codes)
	first := ""
	created := 0
	for code := range codes {
		if code == 202 {
			created++
		} else if code != 200 {
			t.Errorf("unexpected HTTP %d", code)
		}
	}
	for got := range ids {
		if got == "" {
			t.Error("missing job ID")
		}
		if first == "" {
			first = got
		} else if got != first {
			t.Errorf("two jobs: %s and %s", first, got)
		}
	}
	if created != 1 {
		t.Errorf("created=%d, want 1", created)
	}
	var count int
	if err = db.QueryRow(`SELECT COUNT(*) FROM jobs WHERE owner_id=? AND idempotency_key=?`, who, key).Scan(&count); err != nil || count != 1 {
		t.Fatalf("count=%d error=%v", count, err)
	}
}
