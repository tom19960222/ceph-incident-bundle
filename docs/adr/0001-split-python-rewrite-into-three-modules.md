# 將 Python 重寫切成三個模組

Python 重寫採用三個模組：公開入口負責 CLI、inventory、收集編排、workdir 生命週期、incident bundle 打包與驗證；工作機端 collectors 負責 Ceph、Rook、Prometheus 與共用的 command/capture/manifest 行為；自足的 node collector 則是唯一透過 SSH 傳到 node 執行的程式，包含 node 與 `/var/log` 收集。這個切法讓「工作機與 node」的部署 seam 清楚可見，也避免兩檔方案把工作機端程式膨脹成難以閱讀與 review 的巨型模組，同時仍符合 airgap 環境只能手動複製少量檔案的限制。

## Consequences

- airgap 部署需要攜帶三個 Python 檔案，而不是兩個。
- node collector 必須保持自足；必要時可以重複少量底層函式，不能依賴工作機端 collectors。
- 不建立包羅各種 helper 的 `common.py`。共用行為應由真正擁有該政策的模組吸收。
