---
id: example-dependency-timeout
title: Illustrative dependency timeout investigation
services: [example-api]
error_classes: [TimeoutError]
failure_layers: [network, database, application]
---

## Diagnosis
Inspect dependency latency, timeout configuration and request-linked traces for the same
incident window. Supporting result: dependency duration exceeds the configured timeout.
Weakening result: the dependency completes within the timeout and failure occurs earlier.
A timeout alone does not establish the failing layer or dependency involvement.

## Prerequisites
Confirm environment, affected service and exact dependency. Obtain read access to metrics
and traces. Any configuration change requires an agreed limit, owner and rollback value.

## Mitigation
If a confirmed configuration error caused the incident, an operator may restore the
previous known-good configuration through the service's normal deployment process.
The previous value and deployment procedure are unknown in this illustrative example.

## Risks
Changing timeouts can increase resource use and hide downstream degradation. Deployment
may interrupt example-api. Verify capacity and request retry behavior before any change.

## Rollback
Restore the recorded pre-change configuration through the same deployment process if
errors, latency or resource use worsen. Escalate when the prior state is unavailable.

## Verification
Measure request error rate, latency and dependency health against the service's agreed
objectives. Success thresholds and the required observation period are unknown in this
example and must be established by the operator. Record measured results and timestamps.
