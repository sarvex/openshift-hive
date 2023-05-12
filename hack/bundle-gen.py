#!/usr/bin/env python

import argparse
import datetime
import git
import github as gh
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib3
import yaml

import version

HIVE_REPO_DEFAULT = "git@github.com:openshift/hive.git"

# Hive dir within both:
# https://github.com/redhat-openshift-ecosystem/community-operators-prod
# https://github.com/k8s-operatorhub/community-operators
HIVE_SUB_DIR = "operators/hive-operator"

OPERATORHUB_HIVE_IMAGE_DEFAULT = "quay.io/openshift-hive/hive"

REGISTRY_AUTH_FILE_DEFAULT = f'{os.environ["HOME"]}/.docker/config.json'

COMMUNITY_OPERATORS_HIVE_PKG_URL = "https://raw.githubusercontent.com/redhat-openshift-ecosystem/community-operators-prod/main/operators/hive-operator/hive.package.yaml"

SUBPROCESS_REDIRECT = subprocess.DEVNULL

HIVE_BRANCH_DEFAULT = version.HIVE_BRANCH_DEFAULT

CHANNEL_DEFAULT = version.CHANNEL_DEFAULT


def get_params():
    parser = argparse.ArgumentParser(
        description="Hive Bundle Generator and Publishing Script"
    )
    parser.add_argument(
        "--verbose",
        default=False,
        help="Show more details while running",
        action="store_true",
    )
    parser.add_argument(
        "--dry-run",
        default=False,
        help="Test run that skips pushing branches and submitting PRs",
        action="store_true",
    )
    parser.add_argument(
        "--branch",
        default=HIVE_BRANCH_DEFAULT,
        help="""The branch (or commit-ish) from which to build and push the image.
                                If unspecified, we assume `{}`. If we're using `{}`, we
                                generate the bundle version number based on the hive version prefix `{}` and
                                update the `{}` channel. If BRANCH *also* corresponds to an `ocm-X.Y`,
                                we'll also update that channel with the same bundle. If BRANCH corresponds
                                to an `ocm-X.Y` but *not* `{}`, we'll generate the bundle version
                                number based on `X.Y` and update only that channel. A BRANCH named
                                `ocm-X.Y-mce-M.N` will result in a bundle with semver prefix `X.Y` in a
                                channel named `mce-M.N`.""".format(
            HIVE_BRANCH_DEFAULT,
            HIVE_BRANCH_DEFAULT,
            version.HIVE_VERSION_PREFIX,
            CHANNEL_DEFAULT,
            HIVE_BRANCH_DEFAULT,
        ),
    )
    parser.add_argument(
        "--registry-auth-file",
        default=REGISTRY_AUTH_FILE_DEFAULT,
        help="Path to registry auth file",
    )
    # TODO: Validate this early! As written, if this is wrong you won't bounce until open_pr.
    parser.add_argument(
        "--github-user",
        default=os.getenv("GITHUB_USER") or os.environ["USER"],
        help="User's github username. Defaults to $GITHUB_USER, then $USER.",
    )
    parser.add_argument(
        "--hold",
        default=False,
        help='Adds a /hold comment in commit body to prevent the PR from merging (use "/hold cancel" to remove)',
        action="store_true",
    )
    parser.add_argument(
        "--hive-repo",
        default=HIVE_REPO_DEFAULT,
        help="The hive git repository to clone. E.g. save time by using a local directory (but make sure it's up to date!)"
    )
    args = parser.parse_args()

    if shutil.which("buildah"):
        args.build_engine = "buildah"
    elif shutil.which("docker"):
        args.build_engine = "docker"
    else:
        print("neither buildah nor docker found, please install one or the other.")
        sys.exit(1)

    if not os.path.isfile(args.registry_auth_file):
        parser.error(
            f"--registry-auth-file ({args.registry_auth_file}) does not exist, provide --registry-auth-file"
        )

    if args.verbose:
        global SUBPROCESS_REDIRECT
        SUBPROCESS_REDIRECT = None

    return args


# build_and_push_image uses buildah or docker to build the HIVE image from the current
# working directory (tagged with "v{hive_version}" eg. "v1.2.3187-18827f6") and then
# pushes the image to quay.
def build_and_push_image(registry_auth_file, hive_version, dry_run, build_engine):
    container_name = f"{OPERATORHUB_HIVE_IMAGE_DEFAULT}:v{hive_version}"

    if dry_run:
        print(f"Skipping build of container {container_name} due to dry-run")
        return

    if build_engine == "buildah":
        build = dict(
            query="buildah images -nq {}",
            build="buildah bud --tag {} -f ./Dockerfile",
            push="buildah push --authfile={} {}",
        )
        registry_auth_arg = registry_auth_file
    elif build_engine == "docker":
        build = dict(
            query="docker image inspect {}",
            build="docker build --tag {} -f ./Dockerfile .",
            push="docker --config {} push {}",
        )
        registry_auth_arg = os.path.dirname(registry_auth_file)

    # Did we already build it locally?
    cp = subprocess.run(
        build['query'].format(container_name).split(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if cp.returncode == 0:
        print(f"Container {container_name} already exists locally; not rebuilding.")
    else:
        # build/push the thing
        print(f"Building container {container_name}")

        cmd = build['build'].format(container_name).split()
        subprocess.run(cmd, check=True)

    print("Pushing container")
    subprocess.run(
        build['push'].format(registry_auth_arg, container_name).split(),
        check=True,
    )


# get_previous_version grabs the previous hive version (without the leading `v`) from
# COMMUNITY_OPERATORS_HIVE_PKG_URL package yaml for the provided channel_name.
def get_previous_version(channel_name):
    http = urllib3.PoolManager()
    r = http.request("GET", COMMUNITY_OPERATORS_HIVE_PKG_URL)
    pkg_yaml = yaml.load(r.data.decode("utf-8"), Loader=yaml.FullLoader)
    try:
        for channel in pkg_yaml["channels"]:
            if channel["name"] == channel_name:
                return channel["currentCSV"].replace("hive-operator.v", "")
    except:
        print(
            "Unable to determine previous hive version from {}",
            COMMUNITY_OPERATORS_HIVE_PKG_URL,
        )
        raise

    # Channel not found -- no previous version.
    return None

# generate_csv_base generates a hive bundle from the current working directory
# and deposits all artifacts in the specified bundle_dir.
# If prev_version is not None, the CSV will include it as `replaces`.
def generate_csv_base(bundle_dir, version, prev_version):
    print(f"Writing bundle files to directory: {bundle_dir}")
    print(f"Generating CSV for version: {version}")

    crds_dir = "config/crds"
    csv_template = "config/templates/hive-csv-template.yaml"
    operator_role = "config/operator/operator_role.yaml"
    deployment_spec = "config/operator/operator_deployment.yaml"

    # The bundle directory doesn't have the 'v'
    version_dir = os.path.join(bundle_dir, version)
    if not os.path.exists(version_dir):
        os.mkdir(version_dir)

    owned_crds = []

    # Copy all CSV files over to the bundle output dir:
    crd_files = sorted(os.listdir(crds_dir))
    for file_name in crd_files:
        full_path = os.path.join(crds_dir, file_name)
        if os.path.isfile(os.path.join(crds_dir, file_name)):
            dest_path = os.path.join(version_dir, file_name)
            shutil.copy(full_path, dest_path)
            # Read the CRD yaml to add to owned CRDs list
            with open(dest_path, "r") as stream:
                crd_csv = yaml.load(stream, Loader=yaml.SafeLoader)
                owned_crds.append(
                    {
                        "description": crd_csv["spec"]["versions"][0]["schema"][
                            "openAPIV3Schema"
                        ]["description"],
                        "displayName": crd_csv["spec"]["names"]["kind"],
                        "kind": crd_csv["spec"]["names"]["kind"],
                        "name": crd_csv["metadata"]["name"],
                        "version": crd_csv["spec"]["versions"][0]["name"],
                    }
                )

    with open(csv_template, "r") as stream:
        csv = yaml.load(stream, Loader=yaml.SafeLoader)

    csv["spec"]["customresourcedefinitions"]["owned"] = owned_crds

    csv["spec"]["install"]["spec"]["clusterPermissions"] = []

    # Add our operator role to the CSV:
    with open(operator_role, "r") as stream:
        operator_role = yaml.load(stream, Loader=yaml.SafeLoader)
        csv["spec"]["install"]["spec"]["clusterPermissions"].append(
            {"rules": operator_role["rules"], "serviceAccountName": "hive-operator",}
        )

    # Add our deployment spec for the hive operator:
    with open(deployment_spec, "r") as stream:
        operator = yaml.load_all(stream, Loader=yaml.SafeLoader)
        operator_components = list(operator)
        operator_deployment = operator_components[1]
        csv["spec"]["install"]["spec"]["deployments"][0]["spec"] = operator_deployment[
            "spec"
        ]

    # Update the versions to include git hash:
    csv["metadata"]["name"] = f"hive-operator.v{version}"
    csv["spec"]["version"] = version
    if prev_version is not None:
        csv["spec"]["replaces"] = f"hive-operator.v{prev_version}"

    # Update the deployment to use the defined image:
    image_ref = f"{OPERATORHUB_HIVE_IMAGE_DEFAULT}:v{version}"
    csv["spec"]["install"]["spec"]["deployments"][0]["spec"]["template"]["spec"][
        "containers"
    ][0]["image"] = image_ref
    csv["metadata"]["annotations"]["containerImage"] = image_ref

    # Set the CSV createdAt annotation:
    now = datetime.datetime.now()
    csv["metadata"]["annotations"]["createdAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Write the CSV to disk:
    csv_filename = f"hive-operator.v{version}.clusterserviceversion.yaml"
    csv_file = os.path.join(version_dir, csv_filename)
    with open(csv_file, "w") as outfile:
        yaml.dump(csv, outfile, default_flow_style=False)
    print(f"Wrote ClusterServiceVersion: {csv_file}")


def generate_package(package_file, channel, version):
    document_template = """
      channels:
      - currentCSV: %s
        name: %s
      defaultChannel: %s
      packageName: hive-operator
"""
    name = f"hive-operator.v{version}"
    document = document_template % (name, channel, channel)

    with open(package_file, "w") as outfile:
        yaml.dump(
            yaml.load(document, Loader=yaml.SafeLoader),
            outfile,
            default_flow_style=False,
        )
    print(f"Wrote package: {package_file}")


def open_pr(
    work_dir,
    fork_repo,
    upstream_repo,
    gh_username,
    bundle_source_dir,
    new_version,
    prev_version,
    update_channels,
    hold,
    dry_run,
):

    dir_name = fork_repo.split("/")[1][:-4]

    dest_github_org = upstream_repo.split(":")[1].split("/")[0]
    dest_github_reponame = dir_name

    os.chdir(work_dir)

    print()
    print()
    print(f"Cloning {fork_repo}")
    repo_full_path = os.path.join(work_dir, dir_name)
    # clone git repo
    try:
        git.Repo.clone_from(fork_repo, repo_full_path)
    except:
        print(f"Failed to clone repo {fork_repo} to {repo_full_path}")
        raise

    # get to the right place on the filesystem
    print(f"Working in {repo_full_path}")
    os.chdir(repo_full_path)

    repo = git.Repo(repo_full_path)
    try:
        repo.create_remote("upstream", upstream_repo)
    except:
        print("Failed to create upstream remote")
        raise

    print("Fetching latest upstream")
    try:
        repo.remotes.upstream.fetch()
    except:
        print("Failed to fetch upstream")
        raise

    # Starting branch
    print("Checkout latest upstream/main")
    try:
        repo.git.checkout("upstream/main")
    except:
        print("Failed to checkout upstream/main")
        raise

    branch_name = f"update-hive-{new_version}"

    print(f"Create branch {branch_name}")
    try:
        repo.git.checkout("-b", branch_name)
    except:
        print(f"Failed to checkout branch {branch_name}")
        raise

    # copy bundle directory
    print("Copying bundle directory")
    bundle_files = os.path.join(bundle_source_dir, new_version)
    hive_dir = os.path.join(repo_full_path, HIVE_SUB_DIR, new_version)
    shutil.copytree(bundle_files, hive_dir)

    # update bundle manifest
    print("Updating bundle manfiest")
    bundle_manifests_file = os.path.join(
        repo_full_path, HIVE_SUB_DIR, "hive.package.yaml"
    )
    bundle = {}
    with open(bundle_manifests_file, "r") as a_file:
        bundle = yaml.load(a_file, Loader=yaml.SafeLoader)

    found = False
    for channel in bundle["channels"]:
        if channel["name"] in update_channels:
            found = True
            channel["currentCSV"] = f"hive-operator.v{new_version}"

    if prev_version is None:
        # New channel! Sanity check a couple things.
        if found:
            print(
                f"Unexpectedly got prev_version==None but found at least one of the following channels: {update_channels}"
            )
            sys.exit(1)
        if len(update_channels) != 1:
            print(f"Expected exactly one channel name (got [{update_channels}])!")
            sys.exit(1)
        # All good.
        print(f"Adding new channel {update_channels[0]}")
        # TODO: sort?
        bundle["channels"].append(
            {
                "name": update_channels[0],
                "currentCSV": f"hive-operator.v{new_version}",
            }
        )
        pr_title = f"Create channel {update_channels[0]} for Hive community operator at {new_version}"
    else:
        if not found:
            print("did not find a CSV channel to update")
            sys.exit(1)
        pr_title = f"Update Hive community operator channel(s) [{update_channels}] to {new_version}"

    with open(bundle_manifests_file, "w") as outfile:
        yaml.dump(bundle, outfile, default_flow_style=False)
    print("\nUpdated bundle package:\n\n")
    cmd = f"cat {bundle_manifests_file}".split()
    subprocess.run(cmd)
    print()

    # commit files
    print("Adding file")
    repo.git.add(HIVE_SUB_DIR)

    print(f"Committing {pr_title}")
    try:
        repo.git.commit("--signoff", f"--message={pr_title}")
    except:
        print("Failed to commit")
        raise
    print()

    if not dry_run:
        print(f"Pushing branch {branch_name}")
        origin = repo.remotes.origin
        try:
            origin.push(branch_name, None, force=True)
        except:
            print("failed to push branch to origin")
            raise

        # open PR
        client = gh.GitHubClient(dest_github_org, dest_github_reponame, "")

        from_branch = f"{gh_username}:{branch_name}"
        to_branch = "main"

        body = pr_title
        if hold:
            body = "%s\n\n/hold" % body

        resp = client.create_pr(from_branch, to_branch, pr_title, body)
        if resp.status_code != 201:  # 201 == Created
            print(resp.text)
            sys.exit(1)

        json_content = json.loads(resp.content.decode("utf-8"))
        print(f'PR opened: {json_content["html_url"]}')

    else:
        print("Skipping branch push due to dry-run")
    print()


if __name__ == "__main__":
    args = get_params()

    hive_repo_dir = tempfile.TemporaryDirectory(prefix="hive-repo-")

    print(f"Cloning {args.hive_repo} to {hive_repo_dir.name}")
    try:
        git.Repo.clone_from(args.hive_repo, hive_repo_dir.name)
    except:
        print(f"Failed to clone repo {args.hive_repo} to {hive_repo_dir.name}")
        raise

    hive_repo = git.Repo(hive_repo_dir.name)

    hive_commit, hive_version_prefix, update_channels = version.process_branch(
        hive_repo, args.branch
    )

    # The channel we use when looking for stuff in the package.yaml file
    channel = (
        CHANNEL_DEFAULT if CHANNEL_DEFAULT in update_channels else update_channels[0]
    )

    bundle_dir = tempfile.TemporaryDirectory(prefix="hive-operator-bundle-")
    work_dir = tempfile.TemporaryDirectory(prefix="operatorhub-push-")

    print(f"Working in {hive_repo_dir.name}")
    os.chdir(hive_repo_dir.name)

    print(f"Checking out {hive_commit}")
    try:
        hive_repo.git.checkout(hive_commit)
    except:
        print(f"Failed to checkout {hive_commit}")
        raise

    hive_version = version.gen_hive_version(hive_repo, hive_commit, hive_version_prefix)
    prev_version = get_previous_version(channel)
    if hive_version == prev_version:
        raise ValueError(f"Version {hive_version} already exists upstream")

    build_and_push_image(args.registry_auth_file, hive_version, args.dry_run, args.build_engine)
    generate_csv_base(bundle_dir.name, hive_version, prev_version)
    generate_package(os.path.join(bundle_dir.name, "hive.package.yaml"), channel, hive_version)

    # redhat-openshift-ecosystem/community-operators-prod
    open_pr(
        work_dir.name,
        f"git@github.com:{args.github_user}/community-operators-prod.git",
        "git@github.com:redhat-openshift-ecosystem/community-operators-prod.git",
        args.github_user,
        bundle_dir.name,
        hive_version,
        prev_version,
        update_channels,
        args.hold,
        args.dry_run,
    )
    # k8s-operatorhub/community-operators
    open_pr(
        work_dir.name,
        f"git@github.com:{args.github_user}/community-operators.git",
        "git@github.com:k8s-operatorhub/community-operators.git",
        args.github_user,
        bundle_dir.name,
        hive_version,
        prev_version,
        update_channels,
        args.hold,
        args.dry_run,
    )

    hive_repo_dir.cleanup()
    bundle_dir.cleanup()
    work_dir.cleanup()
