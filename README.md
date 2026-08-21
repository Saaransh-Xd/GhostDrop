<img src="assets/banner.png" alt="Banner" width="100%">

GhostDrop is a fast, simple, and lightweight sharing service for files, folders, and text.

Upload files, create text pastes, share folders, use custom slugs, protect your uploads with passwords, and get a shareable link instantly.

No accounts. No unnecessary BS. upload, paste, and share.

## Storage

GhostDrop uses the local `uploads` directory by default. To use AWS S3 or any
S3-compatible provider such as Cloudflare R2, install the dependencies and set
these variables in `src/.env`:

```env
STORAGE_BACKEND=s3
S3_BUCKET=your-bucket
S3_REGION=auto
S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
S3_ADDRESSING_STYLE=auto
S3_ACCESS_KEY_ID=your-access-key
S3_SECRET_ACCESS_KEY=your-secret-key
S3_PREFIX=ghostdrop
```

For AWS S3, leave `S3_ENDPOINT_URL` empty and use the bucket's AWS region.
Use `S3_ADDRESSING_STYLE=path` for providers that require path-style bucket
URLs (some MinIO deployments do).
`STORAGE_BACKEND=r2`, `minio`, or `s3-compatible` are also accepted aliases.
Object keys are stored under the configured prefix; metadata remains local in
`uploads_meta` so existing expiry, password, and view-count behavior is kept.

<div align="center">
A product by
<a href="https://mizucode.qzz.io">
<img src="assets/org.png" alt="The Mizu Code Project" width="400">
</a>
