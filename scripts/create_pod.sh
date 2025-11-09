#!/bin/sh
ACCESS_TOKEN=`curl -s -X POST "http://127.0.0.1:8000/token" \
     -d "username=user&password=password" \
     -H "Content-Type: application/x-www-form-urlencoded" | jq -r '.access_token'`
TASK_ID=`curl -s -X POST http://localhost:8000/create-pods/ \
   -H "Content-Type: application/json" \
   -H "Authorization: Bearer $ACCESS_TOKEN" \
   --data @create_pod.json`
sleep 10
curl -s -X GET "http://127.0.0.1:8000/task/"$TASK_ID -H "Authorization: Bearer $ACCESS_TOKEN"   -H "Content-Type: application/json" | jq -r '.result' 
