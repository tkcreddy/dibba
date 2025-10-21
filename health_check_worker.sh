#!/bin/sh
celery -A utils.celery.health_check_worker worker -l info
