# Cost assumptions and teardown

This is the October 5, 2026 lab estimate, not a current price quote or an actual
final bill. Recheck regional prices, credits, and existing account resources
before provisioning. An alert or an approved allowance is **not a spending cap**.
The approved lab allowance was $5 AWS plus $1 model usage, excluding tax and
preexisting resources.

## One-day estimate: us-east-2

| Component | Lab assumption | Approximate one-day cost |
|---|---|---:|
| RDS compute | Single-AZ `db.t4g.micro`; $0.016/hour | $0.38 |
| RDS storage | 20 GiB gp3; $0.115/GiB-month, 30-day approximation | $0.08 |
| Fargate | One Linux/x86_64 task, 0.5 vCPU + 1 GiB | $0.59 |
| Public IPv4 | One task address; $0.005/hour | $0.12 |
| Secrets Manager | TLS, application database, RDS-managed administrator secret; $0.40/secret-month prorated, plus requests | About $0.04 plus requests |
| ECR | Approximately 0.094 GB image; $0.10/GB-month | Less than $0.01 |
| S3 | One 2.3 KB PDF plus PUT/HEAD/GET requests | Less than $0.01 |
| CloudWatch | Small log volume; one-day retention | Small usage-dependent charge |
| Extra tasks | Short migration/bootstrap/key/data checks; brief replacement overlap | Small additional compute/IP charge |
| ALB / NAT / paid VPC endpoints / AWS Private CA | Not created | $0 |
| Snapshots / retained backups | No snapshots created; automated retention 0 | $0 under this setup; retained snapshots later cost extra |
| Model API | Gemini embedding/chat requests | Separate provider charge; depends on model, token usage, and quotas |

Planning allowance: roughly **$1.30-$2 for a one-day AWS lab**, plus model usage,
tax, and preexisting account charges. RDS burstable CPU credits, data transfer,
unexpected retries, additional logs, and longer retention can add charges. The
existing model secret was reused and is not a new lab-created secret.

Infrastructure continues billing while the API is idle. Stopping the service is
not teardown. Stopped RDS still bills for storage and automatically restarts
after seven days. See [RDS stopping behavior](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_StopInstance.html),
[Fargate pricing](https://aws.amazon.com/fargate/pricing/),
[RDS PostgreSQL pricing](https://aws.amazon.com/rds/postgresql/pricing/),
[VPC pricing](https://aws.amazon.com/vpc/pricing/), and
[Secrets Manager pricing](https://aws.amazon.com/secrets-manager/pricing/).

## Complete teardown checklist

**Status: not executed or verified at the recorded checkpoint.** Perform deletion
incrementally and inspect each result. Use a resource manifest and exact IDs;
do not use broad name-pattern deletion against an account with other projects.

For each destructive step, explain the impact and any continuing cost first.
Deleting compute is recoverable from source/configuration; deleting database
and object data may be permanent unless a backup was deliberately retained.

1. **Preserve non-secret evidence.** Save responses, artifact/source digests,
   resource IDs for cleanup, and persistence results. Decide explicitly whether
   any data must survive. This lab uses only synthetic data.
2. **Remove ECS compute.** Set the lab service desired count to zero, verify all
   service tasks are STOPPED, and delete the service. List any standalone lab
   tasks and stop only those still running. Deregister lab task-definition
   revisions and delete the empty cluster. Verify task network interfaces/public
   IP associations disappear. Scaling to zero is only the first cleanup step.
3. **Delete RDS deliberately.** Delete the lab instance. For disposable synthetic
   data, explicitly skip the final snapshot and do not retain automated backups.
   Wait until the instance is absent. Check manual snapshots, retained automated
   backups, and any AWS Backup recovery points; delete only lab-created ones if
   retention is not intended. A retained snapshot continues billing. Verify the
   RDS-managed administrator secret was removed as part of instance deletion;
   do not delete shared encryption keys.
4. **Delete the original-PDF storage.** Empty the lab bucket, including object
   versions/delete markers if versioning was enabled later. Abort incomplete
   multipart uploads. Delete the empty bucket and confirm it is absent. S3
   deletion cannot be reversed without another copy.
5. **Remove lab application/TLS secrets.** Delete only the lab-created database
   and TLS secrets. Select the intended recovery-window/permanent-deletion
   behavior explicitly; permanent deletion cannot be undone. Preserve the
   preexisting model-provider secret and unrelated secrets. Check that all lab
   secret metadata reaches the intended deletion state.
6. **Remove ECR storage.** Delete the lab repository/images after no task uses
   them. Preserve the preexisting application repository and its images. Verify
   the lab repository is absent.
7. **Remove logs and optional access resources.** Delete the lab CloudWatch log
   group, any lab-specific alarms/dashboards, and any extra log groups created
   during troubleshooting. Remove lab-only budget alerts if no longer needed;
   preserve account-wide controls.
8. **Remove IAM roles.** Remove inline/attached lab role policies and delete the
   lab task/execution roles after consumers are gone. Preserve shared roles and
   the ECS service-linked role. Separately review temporary administrator access
   on the human deployment identity; do not delete the operator identity.
9. **Remove networking.** Once RDS and task ENIs are gone, delete the RDS subnet
   group, lab security groups, subnets, custom route tables, internet gateway,
   and VPC in dependency order. Disassociate routes/subnets and detach the
   internet gateway as necessary. Verify no NAT gateway, paid endpoint, ALB,
   listener/target group, or allocated Elastic IP was added during the lab.
   None was part of the verified architecture, but inspect for later additions.
10. **Remove local secret material.** After API access is no longer needed,
    delete the protected tenant-key header and lab TLS material, including the
    local CA private key. Clear any later browser authorization. If browser
    access is added later, remove its hosts entry and exact CA trust/certificate;
    neither was verified as installed at this checkpoint. Keep the public
    synthetic fixture and sanitized evidence.
11. **Verify resources and Billing.** Run read-only inventory checks in Ohio,
    and inspect any region used by troubleshooting. Compare with the pre-lab
    baseline rather than claiming the whole account is empty. Check ECS tasks,
    RDS/backups, S3, ECR, secrets, logs, ENIs/public IPv4, and networking. Then
    inspect Billing/Cost Explorer by service and region after usage data updates;
    historical charges remain visible and reporting can lag deletion. Check
    model-provider usage separately. Record what was deleted and what was
    intentionally retained.

A final cleanup result should name retained preexisting resources and confirm
that no lab compute, database, objects, images, logs, billable access resources,
or unintended backups remain. Do not replace this evidence with “service stopped.”
