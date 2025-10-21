#!/bin/sh
celery -A utils.celery.beat beat
