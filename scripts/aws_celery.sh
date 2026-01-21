#!/bin/sh
celery -A utils.celery.aws_worker worker -l info
