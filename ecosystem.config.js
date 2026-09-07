module.exports = {
  apps: [{
    name: 'mangadex-api',
    // 需要 gunicorn 在 PATH 中（先激活 venv 再 pm2 start，或改为绝对路径如 ./venv/bin/gunicorn）
    script: 'gunicorn',
    args: '-w 2 -k gthread --threads 8 -b 0.0.0.0:3001 --timeout 120 index:app',
    interpreter: 'none',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '1G',
    env: {
      PORT: 3001
    },
    error_file: './logs/pm2-error.log',
    out_file: './logs/pm2-out.log',
    log_file: './logs/pm2-combined.log',
    time: true,
    merge_logs: true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
    min_uptime: '10s',
    max_restarts: 10,
    restart_delay: 4000,
    kill_timeout: 5000,
    exp_backoff_restart_delay: 100
  }]
}
