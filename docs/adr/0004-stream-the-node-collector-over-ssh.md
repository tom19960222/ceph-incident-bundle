# 透過 SSH 串流 node collector 與 evidence archive

工作機不再把 node collector 打包、上傳並解壓到 node，而是在單一 SSH 連線中以 stdin 傳送自足的 node collector payload，以 stdout 接收 node evidence archive，並將 stderr 專門保留給進度與診斷。遠端固定 bootstrap 會先檢查 Python 3.11，再從 stdin 讀取原始碼，以 `ceph_incident_node.py` 為檔名 compile/exec；node collector 在暫存目錄收集 evidence，使用外部 tar 與 gzip 將完整 archive 寫入 stdout，最後清理暫存目錄。這個協定移除上傳 tar、遠端解壓與入口檔定位，同時讓 traceback 保留可對照的來源檔名。

ADR 0013 後續把 runtime floor 下修為 Python 3.10；本 ADR 保留上述歷史
決策，single-SSH transport 與 stream 分工不變。

## Consequences

- Node collector payload 必須自足，不能 import 只存在工作機上的模組。
- stdout 從第一個 byte 到最後一個 byte 都保留給 gzip archive；所有其他輸出必須走 stderr 或 artifact 檔案。
- Node collector 可以先回傳有效 archive 再以非零狀態結束，讓工作機保留已取得的 evidence 並把整體結果標成 partial。
- 被中斷或損毀的 archive 仍視為不可用；工作機只接受可完整解開且含 manifest 的 node evidence archive。
