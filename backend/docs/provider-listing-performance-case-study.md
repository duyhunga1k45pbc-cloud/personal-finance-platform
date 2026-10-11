# Provider transaction listing: N+1 case study

## Kết quả chính

Trên cùng dataset PostgreSQL 16 có 1.000 provider transactions và 1.000
evidence rows, phép đo ghép cặp trước/sau ghi nhận:

| Chỉ số | Trước (per-row COUNT) | Sau (set-based aggregate) |
|---|---:|---:|
| SQL statements/request | 1.003 | 3 |
| Request latency p50 | 502,912 ms | 14,223 ms |
| Request latency p95 | 564,066 ms | 23,953 ms |

Mỗi implementation có một warm-up và 25 request đo; thứ tự được đan xen ngẫu
nhiên theo cặp. Mọi response trong 25 cặp có nội dung giống nhau, đủ 1.000
rows và giữ thứ tự `id ASC`. p99 không được báo cáo vì số mẫu quá nhỏ.

Đây là kết quả đo được của harness in-process, không phải latency qua một
deployment/network. DB execute time được tính từ wall time quanh lệnh cursor
execute; không đồng nhất nó với PostgreSQL CPU time.

Harness tái lập đã được chạy lại như một lượt kiểm tra riêng. Lượt đó cũng đo
được 1.003 → 3 statements/request và response tương đương; p50 là 531,060 ms →
16,182 ms, p95 là 586,473 ms → 27,503 ms. Đây là lượt khác, không thay thế số
đo paired chính phía trên; độ lệch cho thấy latency dao động giữa các lượt.

## Nguyên nhân và thay đổi

Trước đây endpoint lấy danh sách transactions rồi chạy một `COUNT` cho từng
row: một query kiểm tra user, một query kiểm tra connection, một query lấy rows
và 1.000 count queries. Phiên bản mới nhóm evidence theo transaction ID trong
một subquery, giới hạn subquery vào provider connection hiện tại, rồi kết nối
kết quả với transactions bằng `LEFT OUTER JOIN`. `COALESCE` trả `0` khi evidence
không có. Ownership check, trường response và `id ASC` không đổi.

## Môi trường và tái lập

- PostgreSQL **16.14**, schema revision `j0a8b5c2e366`.
- Benchmark DB: `pfp_perf_benchmark`; role: `pfp_bench_app`; endpoint host
  `127.0.0.1:55432`. Không ghi mật khẩu, URL chứa mật khẩu, user IDs hay dữ liệu
  tài chính vào tài liệu hoặc kết quả.
- Dataset: 1.000 `external_transactions`, 1.000
  `external_transaction_evidence`; một provider connection.
- Chạy từ `backend/` với môi trường Python backend đã cài dependencies. Với
  integration tests, `DATABASE_URL` phải trỏ tới PostgreSQL 16 disposable test
  DB đã migrate, không phải benchmark DB hoặc database ứng dụng. Với benchmark,
  script chỉ chấp nhận benchmark target nêu trên. Đặt hai URL trong môi trường
  cục bộ; không lưu giá trị hoặc credentials vào repository:

```sh
DATABASE_URL="$PFP_DISPOSABLE_TEST_DATABASE_URL" \
  python -m pytest -q tests/test_provider_evidence.py
DATABASE_URL="$PFP_BENCH_DATABASE_URL" \
  python scripts/benchmark_provider_listing.py
```

Benchmark script xác minh host/port/database/role, phiên bản PostgreSQL và
cardinality trước khi đo; connection được đặt
`default_transaction_read_only=on`, `statement_timeout=30s` và
`lock_timeout=5s`. Script từ chối target khác benchmark DB, số liệu ngoài 1k,
response sai khác, statement count vượt giới hạn, hoặc statement count không
đúng kỳ vọng (1.003/3). Script chỉ gửi GET; không seed, migrate hay ghi dữ liệu.
Nó in JSON gồm cấu hình, số mẫu và p50/p95/mean, để có thể lưu output bên ngoài
repository khi tái chạy.

13 PostgreSQL integration tests đã pass trên instance PostgreSQL 16 disposable
riêng: `13 passed`. Instance test đã được dọn sau khi kiểm thử. Benchmark không
chạy 10k/100k records hoặc tải đồng thời.

## EXPLAIN (ANALYZE, BUFFERS)

Đã chạy trên query set-based với benchmark DB, bằng kết quả đọc-only:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT external_transactions.*,
       COALESCE(anon_1.evidence_count, 0) AS evidence_count
FROM external_transactions
LEFT JOIN (
    SELECT external_transaction_evidence.external_transaction_record_id
               AS transaction_record_id,
           count(external_transaction_evidence.id) AS evidence_count
    FROM external_transaction_evidence
    JOIN external_transactions AS external_transactions_1
      ON external_transactions_1.id =
         external_transaction_evidence.external_transaction_record_id
    WHERE external_transactions_1.provider_connection_id = :connection_id
    GROUP BY external_transaction_evidence.external_transaction_record_id
) AS anon_1
  ON anon_1.transaction_record_id = external_transactions.id
WHERE external_transactions.provider_connection_id = :connection_id
ORDER BY external_transactions.id ASC;
```

Planner chọn `Seq Scan` cho hai bảng, sau đó `Hash Join`, `HashAggregate`,
`Hash Left Join` và sort `quicksort`. Query trả 1.000 rows; sort dùng 118 kB;
execution đọc 61 shared buffers (hit), planning **0,473 ms**, execution
**1,127 ms**. Đây là plan của dataset 1k hiện tại, không phải bằng chứng index
được sử dụng hoặc khuyến nghị thay index.

## Giới hạn và diễn giải

- Baseline historical runs có p50 **484,770 ms** và **543,574 ms**, mỗi lượt
  25 samples nhưng khác thời điểm/điều kiện. Chúng không phải phép so sánh
  paired. Case study dùng số ghép cặp **502,912 → 14,223 ms** nêu trên.
- Transaction-duration numbers bị loại khỏi kết luận: instrumentation trước
  đó chưa được xác thực độc lập.
- Kết quả chỉ mô tả dataset 1k, cache state và môi trường chạy đo. Chưa có
  kết quả về tải đồng thời, dataset lớn, throughput, production network hoặc
  ảnh hưởng dài hạn.
- Không có thay đổi index, financial invariant, transaction logic hoặc cache.
  Cần đo workload đại diện trước khi quyết định tối ưu tiếp.
