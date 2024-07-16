#!/bin/bash
set -eux

# Prepare env vars
ENGINE=${ENGINE:="podman"}
OS=${OS:="rockylinux"}
OS_VERSION=${OS_VERSION:="8"}
PYTHON_VERSION=${PYTHON_VERSION:="3.8"}
ACTION=${ACTION:="test"}
CONTAINER_NAME="atomic-reactor-$OS-$OS_VERSION-py$PYTHON_VERSION"
IMAGE="$OS:$OS_VERSION"

# Use arrays to prevent globbing and word splitting
engine_mounts=(-v "$PWD":"$PWD":z)
for dir in ${EXTRA_MOUNT:-}; do
  engine_mounts=("${engine_mounts[@]}" -v "$dir":"$dir":z)
done

# Create or resurrect container if needed
if [[ $($ENGINE ps -qa -f name="$CONTAINER_NAME" | wc -l) -eq 0 ]]; then
  $ENGINE run --name "$CONTAINER_NAME" -d "${engine_mounts[@]}" -w "$PWD" -ti "$IMAGE" sleep infinity
elif [[ $($ENGINE ps -q -f name="$CONTAINER_NAME" | wc -l) -eq 0 ]]; then
  echo found stopped existing container, restarting. volume mounts cannot be updated.
  $ENGINE container start "$CONTAINER_NAME"
fi

function setup_osbs() {
  # PIP_PREFIX: osbs-client provides input templates that must be copied into /usr/share/...
  ENVS='-e PIP_PREFIX=/usr'
  RUN="$ENGINE exec -i ${ENVS} $CONTAINER_NAME"
  PYTHON="python$PYTHON_VERSION"
  # If the version is e.g. "3.8", the package name is python38
  # DISCLAIMER: Does not work with fedora, stick with python "3"
  PY_PKG="${PYTHON/./}"
  PIP_PKG="$PY_PKG-pip"
  PIP="pip$PYTHON_VERSION"
  PKG="dnf"
  PKG_EXTRA=(flatpak libmodulemd skopeo "$PYTHON")
  if [[ $OS == "rockylinux" ]]; then
    ENABLE_REPO=
  else
    PKG_EXTRA+=(python3-libmodulemd)
    ENABLE_REPO="--enablerepo=updates-testing"
  fi

  # List common install dependencies
  PKG_COMMON_EXTRA=(git-core gcc krb5-devel "$PY_PKG-devel" popt-devel redhat-rpm-config
                    cairo-devel gobject-introspection-devel cairo-gobject-devel ostree)
  PKG_EXTRA+=("${PKG_COMMON_EXTRA[@]}")

  PIP_INST=("$PIP" install --index-url "${PYPI_INDEX:-https://pypi.org/simple}")

  # RPM install basic dependencies
  $RUN $PKG $ENABLE_REPO install -y "${PKG_EXTRA[@]}"

  # Install package
  $RUN $PKG install -y $PIP_PKG

  # Upgrade pip to provide latest features for successful installation
  $RUN "${PIP_INST[@]}" --upgrade "pip<23.1"

  if [[ $OS == rockylinux ]]; then
    # Pip install/upgrade setuptools. Older versions of setuptools don't understand
    # environment markers, also rockylinux needs to have setuptools updates to make
    # pytest-cov work
    $RUN "${PIP_INST[@]}" --upgrade setuptools
    # install with RPM_PY_SYS=true to avoid error caused by installing on system python
    $RUN sh -c "RPM_PY_SYS=true ${PIP_INST[*]} rpm-py-installer"
  fi

  # Pip install packages for atomic-reactor, ignore installed to
  #  avoid conflict with PyGoObject on fedora
  $RUN "${PIP_INST[@]}" -r requirements.txt --ignore-installed

  # Setuptools install atomic-reactor from source
  $RUN $PYTHON setup.py install

  # Pip install packages for unit tests
  $RUN "${PIP_INST[@]}" -r tests/requirements.txt
  $RUN "${PIP_INST[@]}" tox

  # workaround for https://github.com/actions/checkout/issues/766
  $RUN git config --global --add safe.directory "$PWD"
}

case ${ACTION} in
"test")
  setup_osbs
  TEST_CMD="tox -e test"
  ;;
"pylint")
  setup_osbs
  $RUN "${PIP_INST[@]}" 'pylint==2.9.*'
  PACKAGES='atomic_reactor tests'
  TEST_CMD="${PYTHON} -m pylint ${PACKAGES}"
  ;;
"bandit")
  setup_osbs
  $RUN "${PIP_INST[@]}" bandit[baseline]
  TEST_CMD="bandit-baseline -r atomic_reactor -ll -ii"
  ;;
*)
  echo "Unknown action: ${ACTION}"
  exit 2
  ;;
esac

# Run tests
# shellcheck disable=SC2086
$RUN ${TEST_CMD} "$@"

echo "To run tests again:"
echo "$RUN ${TEST_CMD}"
