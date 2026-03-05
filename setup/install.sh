#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Crossplane Capacity Planning Setup ==="

# --- Helper functions ---
wait_for_deployment() {
  local ns="$1" name="$2" timeout="${3:-300}"
  echo "Waiting for deployment $name in $ns (timeout: ${timeout}s)..."
  kubectl rollout status deployment/"$name" -n "$ns" --timeout="${timeout}s"
}

wait_for_condition() {
  local resource="$1" condition="$2" timeout="${3:-300}"
  echo "Waiting for $resource to be $condition (timeout: ${timeout}s)..."
  kubectl wait "$resource" --for="condition=$condition" --timeout="${timeout}s"
}

# --- Step 1: Install Crossplane via Helm ---
echo ""
echo "--- Step 1: Installing Crossplane ---"

# On OpenShift, pre-create namespace and grant anyuid SCC before Helm install
# so pods can start immediately without SCC failures.
grant_ocp_scc() {
  if command -v oc &>/dev/null && oc api-versions 2>/dev/null | grep -q security.openshift.io; then
    echo "OpenShift detected — granting anyuid SCC to crossplane-system namespace..."
    kubectl create namespace crossplane-system 2>/dev/null || true
    # Grant anyuid to ALL service accounts in the namespace. Crossplane providers
    # create dynamic SAs, so individual grants are insufficient.
    oc adm policy add-scc-to-group anyuid system:serviceaccounts:crossplane-system 2>/dev/null || true
  fi
}

if kubectl get namespace crossplane-system &>/dev/null; then
  echo "crossplane-system namespace exists, checking if Crossplane is running..."
  if kubectl get deployment crossplane -n crossplane-system &>/dev/null; then
    # Check if Helm release is healthy
    local_status=$(helm status crossplane -n crossplane-system -o json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('info',{}).get('status',''))" 2>/dev/null || echo "")
    if [ "$local_status" = "failed" ]; then
      echo "Helm release in failed state. Upgrading..."
      grant_ocp_scc
      helm upgrade crossplane crossplane-stable/crossplane \
        -n crossplane-system --wait --timeout 5m
    else
      echo "Crossplane deployment found. Skipping Helm install."
    fi
  else
    echo "Namespace exists but no deployment. Installing Crossplane..."
    grant_ocp_scc
    helm repo add crossplane-stable https://charts.crossplane.io/stable 2>/dev/null || true
    helm repo update crossplane-stable
    helm upgrade --install crossplane crossplane-stable/crossplane \
      -n crossplane-system --create-namespace \
      --wait --timeout 5m
  fi
else
  grant_ocp_scc
  echo "Installing Crossplane via Helm..."
  helm repo add crossplane-stable https://charts.crossplane.io/stable 2>/dev/null || true
  helm repo update crossplane-stable
  helm install crossplane crossplane-stable/crossplane \
    -n crossplane-system --create-namespace \
    --wait --timeout 5m
fi

wait_for_deployment crossplane-system crossplane 300
wait_for_deployment crossplane-system crossplane-rbac-manager 300
echo "Crossplane is running."

# --- Step 2: Create load test namespace ---
echo ""
echo "--- Step 2: Creating load test namespace ---"
kubectl apply -f "$SCRIPT_DIR/00-namespace.yaml"

# --- Step 3: Install provider-nop ---
echo ""
echo "--- Step 3: Installing provider-nop ---"
kubectl apply -f "$SCRIPT_DIR/01-provider-nop.yaml"

echo "Waiting for provider-nop to become Healthy..."
for i in $(seq 1 60); do
  health=$(kubectl get provider provider-nop -o jsonpath='{.status.conditions[?(@.type=="Healthy")].status}' 2>/dev/null || echo "")
  if [ "$health" = "True" ]; then
    echo "provider-nop is Healthy."
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "ERROR: provider-nop did not become Healthy within 5 minutes."
    kubectl get provider provider-nop -o yaml
    exit 1
  fi
  sleep 5
done

# --- Step 4: Apply ProviderConfig (if CRD exists) ---
echo ""
echo "--- Step 4: Applying ProviderConfig ---"
if kubectl get crd providerconfigs.nop.crossplane.io &>/dev/null; then
  kubectl apply -f "$SCRIPT_DIR/02-provider-config.yaml"
else
  echo "ProviderConfig CRD not found (provider-nop v0.5+ doesn't require it). Skipping."
fi

# --- Step 5: Install functions ---
echo ""
echo "--- Step 5: Installing Composition functions ---"
kubectl apply -f "$SCRIPT_DIR/03-functions.yaml"

echo "Waiting for functions to become Healthy..."
for func in function-patch-and-transform function-auto-ready; do
  for i in $(seq 1 60); do
    health=$(kubectl get function "$func" -o jsonpath='{.status.conditions[?(@.type=="Healthy")].status}' 2>/dev/null || echo "")
    if [ "$health" = "True" ]; then
      echo "$func is Healthy."
      break
    fi
    if [ "$i" -eq 60 ]; then
      echo "ERROR: $func did not become Healthy within 5 minutes."
      kubectl get function "$func" -o yaml
      exit 1
    fi
    sleep 5
  done
done

# --- Step 6: Apply XRDs ---
echo ""
echo "--- Step 6: Applying CompositeResourceDefinitions ---"
kubectl apply -f "$PROJECT_DIR/xrds/"

echo "Waiting for XRDs to be Established..."
for xrd in xvmdeployments.capacity.crossplane.io xdisks.capacity.crossplane.io xdnszones.capacity.crossplane.io xfirewallrulesets.capacity.crossplane.io; do
  for i in $(seq 1 60); do
    established=$(kubectl get xrd "$xrd" -o jsonpath='{.status.conditions[?(@.type=="Established")].status}' 2>/dev/null || echo "")
    if [ "$established" = "True" ]; then
      echo "$xrd is Established."
      break
    fi
    if [ "$i" -eq 60 ]; then
      echo "ERROR: $xrd did not become Established within 5 minutes."
      kubectl get xrd "$xrd" -o yaml
      exit 1
    fi
    sleep 5
  done
done

# --- Step 7: Apply Compositions ---
echo ""
echo "--- Step 7: Applying Compositions ---"
kubectl apply -f "$PROJECT_DIR/compositions/"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Installed components:"
echo "  - Crossplane (crossplane-system namespace)"
echo "  - provider-nop v0.5.0"
echo "  - function-patch-and-transform v0.7.0"
echo "  - function-auto-ready v0.3.0"
echo "  - 4 XRDs (VMDeployment, Disk, DNSZone, FirewallRuleSet)"
echo "  - 4 Compositions (NopResource-backed)"
echo "  - crossplane-loadtest namespace"
echo ""
echo "Next steps:"
echo "  make monitor    # Deploy Prometheus rules + Grafana dashboard"
echo "  make test-small # Smoke test with 100 claims"
echo "  make test-full  # Full load test with ~12,500 claims"
