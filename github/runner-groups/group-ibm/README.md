# Summary

This small project defines Github Actions `ibm-runners`. To work in this area requires organizational admin level permissions.

## Deploying Runner Group

To deploy the "infra" you'll need a PAT with the appropriate permissions. Assuming you have one of those, then deployment is the usual "apply".

```bash
terraform apply -var="admin_token=$(cat <path to PAT>)"
```

## Repo Ids

Please use the `get-repo-id` script in `github/tools` directory to get a REPO's unique id.
