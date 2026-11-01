# Hanoi Local Zone residency option

Enabled with `-c enableResidency=true`. Adds a subnet in `ap-southeast-1-han-1a`, SSM endpoints and
an m7i.large tokenizer instance with an encrypted 100 GB data volume (kept on termination;
termination protection on, because the volume holds the only tokenization key).

## Checks before the first transfer (not verifiable without the account)

1. **Gateway endpoints from the Local Zone subnet.** The build adds the Local Zone route table to
   the S3 and DynamoDB gateway endpoints. If the deploy rejects that association, remove the Local
   Zone subnet from the gateway endpoints and set `ADI_S3_ENDPOINT_URL` / `ADI_DDB_ENDPOINT_URL` in
   `/etc/adi/tokenizer.env` to S3 and DynamoDB interface endpoint DNS names.
2. **Package install.** User data runs `dnf install -y python3-boto3 unzip`; the AL2023 repositories
   are reached through the S3 gateway endpoint. If that fails, bake an AMI with the packages.
3. **Key backup.** Back up `/data/key/tokenization.key` with EBS Local Snapshots in the Local Zone.
   Losing the key means old tokens can no longer be matched to new ones; it never means data loss.
4. **Raw extract landing.** Decide how the lender delivers the monthly extract to `/data/raw/YYYY-MM/`
   (SFTP over Direct Connect Hanoi, or a pull job). This is not automated here.
5. **Dry run.** Run `python3 -m adi_residency.tokenizer --month <YYYY-MM>` with the TIA status not
   approved: it must write the local report and upload nothing (exit code 3).
