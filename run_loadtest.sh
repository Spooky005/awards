#!/usr/bin/env bash
# Exit immediately if any command fails
set -e

echo "=== 1. Installing Dependencies ==="
python -m pip install --upgrade pip
pip install -r satuk_agent/requirements.txt
pip install openpyxl

echo "=== 2. Generating YAML configurations ==="
python satuk_agent/loadtest_gen.py --server-id 1 --csv satuk_agent/newdata.xlsx --minutes 30 --clear --nominee AECAS
python satuk_agent/loadtest_gen.py --server-id 2 --csv satuk_agent/newdata.xlsx --minutes 30 --clear --nominee AECAS
python satuk_agent/loadtest_gen.py --server-id 3 --csv satuk_agent/newdata.xlsx --minutes 30 --clear --nominee AECAS
python satuk_agent/loadtest_gen.py --server-id 4 --csv satuk_agent/newdata.xlsx --minutes 30 --clear --nominee AECAS

echo "=== 3. Executing Load Tests Sequentially (Prevents Render Throttling) ==="
# We removed the '&' and 'wait'. They run one by one to stay under CPU/RAM limits.
echo "Running Server 1..."
python satuk_agent/loadtest_run.py --server-id 1 --workers 32 --max-rps 30 --live --allow-real --no-resume --no-seek

echo "Running Server 2..."
python satuk_agent/loadtest_run.py --server-id 2 --workers 16 --max-rps 15 --live --allow-real --no-resume --no-seek

echo "Running Server 3..."
python satuk_agent/loadtest_run.py --server-id 3 --workers 16 --max-rps 15 --live --allow-real --no-resume --no-seek

echo "Running Server 4..."
python satuk_agent/loadtest_run.py --server-id 4 --workers 8  --max-rps 10 --live --allow-real --no-resume --no-seek

echo "=== 4. Uploading CSV Log Results ==="
# Checks if you provided a Webhook URL in Render environment variables
if [ -n "$WEBHOOK_URL" ]; then
  echo "Sending results to webhook..."
  for file in satuk_agent/votes_folder/results_s*.csv; do
    if [ -f "$file" ]; then
      curl -F "file=@$file" "$WEBHOOK_URL"
    fi
  done
  echo "Logs successfully sent!"
else
  echo "WARNING: WEBHOOK_URL variable not set. Logs will be permanently lost when Render spins down."
fi

echo "=== Loadtest Complete ==="
