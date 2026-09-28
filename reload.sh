ssh pafau@192.168.1.123 'tmux kill-session -t spark-dashboard 2>/dev/null || true' && \
rsync -az --delete \
  --exclude '.git/' \
  --exclude '.DS_Store' \
  /Users/pafau/sandbox/spark-dashboard/ \
  pafau@192.168.1.123:~/spark-dashboard/ && \
ssh pafau@192.168.1.123 'cd ~/spark-dashboard && tmux new -d -s spark-dashboard "python3 server/dev_server.py --host 0.0.0.0 --port 8088"'
