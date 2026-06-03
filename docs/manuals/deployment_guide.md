---
title: Deployment Guide
entity: Internal
tags: [deployment, ops]
---

# Deployment Guide

## Prerequisites

Docker and Docker Compose installed, and a populated `.env` file.

## Steps

1. Build and start the stack with `docker compose up --build`.
2. Wait for the Qdrant health check to pass and the API to report ready.
3. Ingest documents, then run a test query against `/query`.
