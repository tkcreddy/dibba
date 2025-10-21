#!/bin/sh
ACCESS_TOKEN=`curl -s -X POST "http://127.0.0.1:8000/token" \
     -d "username=user1&password=password1" \
     -H "Content-Type: application/x-www-form-urlencoded" | jq -r '.access_token'`
#echo $ACCESS_TOKEN
curl -s -X GET "http://127.0.0.1:8000/task/"$1 -H "Authorization: Bearer $ACCESS_TOKEN"   -H "Content-Type: application/json" | jq -r '.result' | jq -r '.Instances[] | {InstanceId, PrivateIpAddress}'
