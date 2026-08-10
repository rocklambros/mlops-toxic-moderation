# Evidence: the stack survives a full stop/start cycle

**Executed 2026-08-10 against the live deployed stack.** Delivery spec §12 requires the
published URL to be reachable after a stop/start cycle. Until now that was a claim about a
mechanism — a systemd unit and a health-gated bring-up — rather than an observation. This is
the observation.

It also found two real defects, and neither would have been found any other way. They are in
the second half of this document, because the rehearsal being useful is the point of running
it while the system works.

## The cycle

| Step | Time (UTC) | Result |
|---|---|---|
| `make db-dump` | 19:47:59 | 685 KB custom-format archive to S3, verified restorable |
| `make aws-down` | 19:48:25 | Dumped again, then stopped three instances and RDS |
| RDS reached `stopped` | 19:59:27 | ~11 minutes |
| Endpoints while down | 19:51 | `curl` on 8000 and 8501 → **connection refused** |
| `make aws-up` | 20:29:56 | Exit **0**, all three endpoints healthy |

**Total time from stop to healthy: about 41 minutes**, of which ~11 was RDS stopping and most
of the rest was RDS starting. The instances themselves are back in a couple of minutes.

## What was preserved

Row counts read from the production database through the backend instance, after the cycle:

| Table | Before | After |
|---|---|---|
| `predictions` | 2033 | **2033** |
| `feedback` | — | 797 |
| `review_queue` | — | 665 |

The three Elastic IPs did not move, so the URLs in the submission are the same URLs. The
containers came back **without intervention**: `infra/deploy/toxic-stack.service` brings the
stack up at boot, which is exactly what it exists for, and the endpoints were already
answering before `aws_up.sh` reached its own health gate.

## Defect 1: the dump could not verify itself, and could not have caught the failure it was for

`make aws-down` failed on its first attempt, at the dump's verification step:

```
download failed: s3://<bucket>/db/2026-08-10T19-41-55Z.dump to - [Errno 32] Broken pipe
ssm_run: FATAL: backend: at least one invocation did not reach Success
```

**`make` stopped at the prerequisite and never ran `aws_down.sh`, so nothing was stopped.**
The structural rule — `aws-down: db-dump` as a prerequisite rather than a step — did its job.

The verification was `aws s3 cp ... - | pg_restore --list`. Measured on the backend instance:

| Probe | Result |
|---|---|
| Download the object to a file | exit 0, 685079 bytes |
| `pg_restore --list` on that file | exit 0, 38 TOC entries |
| The piped form | `PIPE_EXITS=1 0` |

`pg_restore` stops as soon as it has parsed the table of contents and closes stdin. `aws s3
cp` is still writing, takes `EPIPE`, exits 1, and `set -o pipefail` fails the script over a
verification that had actually **succeeded**. It stays invisible while the dump fits in the
64 KiB pipe buffer, which is why it survived every earlier rehearsal and appeared once the
graded dataset reached 685 KB.

The second finding is worse, and the first one was hiding it. Truncating the archive to
200000 of 685079 bytes:

| Check on a 29%-complete archive | Result |
|---|---|
| `pg_restore --list` | **exit 0** — reports success |
| `pg_restore --file=/dev/null` | exit 1, `could not read from input file: end of file` |

The TOC of a custom-format archive sits near the front, so `--list` never reads a data block.
The script's own comment says the check exists because "a `pg_dump` that died halfway still
leaves an object behind… `pg_restore --clean` against it drops every table and THEN fails —
the exact data loss the dump exists to prevent". **The chosen check could not detect that.** A
check that passes on a 29%-complete dump is worse than no check, because it is believed.

Fixed by downloading to a file, parsing the TOC, and then restoring the whole archive to
`/dev/null`, which reads and decompresses every block.

## Defect 2: `make aws-up` could never succeed on this fleet

With the dump fixed, the stop succeeded and the start did not:

```
aws_up: FATAL: backend: no boot marker at /toxic/boot/backend
```

`/toxic/boot/*` did not exist for any component, and the backend's console output contains
**zero** occurrences of `TOXIC-USER-DATA-COMPLETE`. The marker code is in the user-data
template. It has simply never run on these instances.

The cause is a deliberate decision working exactly as designed:
`infra/terraform/compute.tf` sets `user_data_replace_on_change = false` and puts `user_data`
under `ignore_changes`, because the SCP denies `ec2:ModifyInstanceAttribute`. When the boot
marker was added to the template, Terraform correctly changed nothing on the running fleet.
`aws_up.sh`'s own comment — "already present from the first boot" — was true of the design
and false of the hardware.

So the documented recovery command, the one `docs/HANDOFF.md` sends the next person to, could
not return zero on the fleet it was written for, and failed only after burning its full
timeout per component.

Fixed by asking the question the marker is a proxy for, when the marker is absent: if the
stack is already serving, a host answering HTTP has finished booting, which is the same claim
the marker makes. The fallback reuses `verify_live.sh` rather than probing anything itself —
a second implementation of "is it up?" is a second thing that can disagree with the deploy,
and `tests/infra/test_aws_up.py::test_the_gate_is_the_same_one_the_deploy_uses` refuses one.
It is deliberately loud: a marker that can be skipped silently stops meaning anything, and a
genuinely replaced instance still needs it to mean something. With no marker **and** nothing
serving, it is still fatal.

## Reproducing

```bash
export AWS_PROFILE=rc-mlops AWS_REGION=us-west-2
make aws-down          # dumps first, and refuses to stop anything without a fresh dump
make aws-up            # gates on three live health endpoints
make deploy-verify
```

Expect roughly forty minutes end to end, nearly all of it RDS. `make aws-up` prints the
missing-boot-marker warning on the current fleet; that is expected and explained above, and
it will stop appearing on any instance that is replaced.
