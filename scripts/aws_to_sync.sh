#!/bin/bash
cd /Users/krishnareddy/PycharmProjects/dibba
aws s3 sync s3://dibba-bucket/code/ . --exclude ".git*" --exclude ".venv*" --exclude ".idea*" --exclude "config*"
