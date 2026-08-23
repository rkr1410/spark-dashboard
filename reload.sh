ssh pafau@HAL-9000.local 'tmux kill-session -t spark-dashboard 2>/dev/null || true' && \
rsync -az --delete \
  --exclude '.git/' \
  --exclude '.DS_Store' \
  /Users/pafau/sandbox/spark-dashboard/ \
  pafau@HAL-9000.local:~/spark-dashboard/ && \
ssh pafau@HAL-9000.local 'cd ~/spark-dashboard && tmux new -d -s spark-dashboard "python3 server/dev_server.py --host 0.0.0.0 --port 8088"'
