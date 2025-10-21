#!/bin/sh
ACCESS_TOKEN=`curl -s -X POST "http://127.0.0.1:8000/token" \
     -d "username=user&password=password" \
     -H "Content-Type: application/x-www-form-urlencoded" | jq -r '.access_token'`
TASK_ID=`curl -s -X POST "http://127.0.0.1:8000/terminate-namespace/" -H "Authorization: Bearer $ACCESS_TOKEN"   -H "Content-Type: application/json"  -d '{ "namespace": "testNamespace" }' | jq -r '.task_id'`
sleep 10
curl -s -X GET "http://127.0.0.1:8000/task/"$TASK_ID -H "Authorization: Bearer $ACCESS_TOKEN"   -H "Content-Type: application/json" | jq -r '.result'
