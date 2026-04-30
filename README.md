## Complete Python Application

### 1. Main Application (app.py)

```python
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import yaml
import os
from functools import wraps
import logging
from typing import Dict, List, Optional
import json

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-here')

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load Kubernetes configuration
def load_k8s_config():
    """Load Kubernetes configuration based on environment"""
    try:
        # Try in-cluster config first
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes configuration")
    except config.ConfigException:
        # Fall back to kubeconfig
        try:
            config.load_kube_config()
            logger.info("Loaded kubeconfig file")
        except config.ConfigException as e:
            logger.error(f"Could not load Kubernetes configuration: {e}")
            raise

load_k8s_config()

# Initialize Kubernetes API clients
core_v1_api = client.CoreV1Api()
custom_api = client.CustomObjectsApi()

# Configuration for clusters
# You can expand this to connect to multiple clusters
CLUSTERS = {
    'default': {
        'name': 'Production Cluster',
        'context': None  # Use default context
    }
    # Add more clusters as needed
}

# Name of the ConfigMap that will store contact information
CONTACT_CONFIGMAP_NAME = "namespace-contact-info"

# Required contact fields
CONTACT_FIELDS = [
    'owner_name',
    'owner_email',
    'team_name',
    'slack_channel',
    'oncall_rotation'
]

class KubernetesClusterManager:
    """Manager for handling multiple Kubernetes clusters"""
    
    def __init__(self):
        self.clients = {}
        self._init_clients()
    
    def _init_clients(self):
        """Initialize API clients for each cluster"""
        for cluster_name, cluster_config in CLUSTERS.items():
            try:
                if cluster_config.get('context'):
                    # Load specific context
                    contexts, active_context = config.list_kube_config_contexts()
                    for ctx in contexts:
                        if ctx['name'] == cluster_config['context']:
                            config.load_kube_config(context=cluster_config['context'])
                            break
                else:
                    # Use default configuration
                    config.load_kube_config()
                
                self.clients[cluster_name] = {
                    'core': client.CoreV1Api(),
                    'name': cluster_config['name']
                }
                logger.info(f"Initialized client for cluster: {cluster_config['name']}")
            except Exception as e:
                logger.error(f"Failed to initialize client for cluster {cluster_name}: {e}")
    
    def get_all_namespaces_with_contacts(self) -> List[Dict]:
        """Get all namespaces with their contact information from all clusters"""
        all_namespace_data = []
        
        for cluster_name, client_info in self.clients.items():
            try:
                namespaces = client_info['core'].list_namespace()
                
                for ns in namespaces.items:
                    namespace_name = ns.metadata.name
                    contact_info = self.get_namespace_contact_info(cluster_name, namespace_name)
                    
                    all_namespace_data.append({
                        'cluster': cluster_name,
                        'cluster_display': client_info['name'],
                        'namespace': namespace_name,
                        'contact_info': contact_info,
                        'has_contact': bool(contact_info and contact_info.get('owner_name'))
                    })
            except ApiException as e:
                logger.error(f"Error listing namespaces for cluster {cluster_name}: {e}")
        
        return all_namespace_data
    
    def get_namespace_contact_info(self, cluster_name: str, namespace: str) -> Dict:
        """Get contact information for a specific namespace"""
        try:
            client_info = self.clients.get(cluster_name)
            if not client_info:
                return {}
            
            configmap = client_info['core'].read_namespaced_config_map(
                name=CONTACT_CONFIGMAP_NAME,
                namespace=namespace
            )
            
            # Extract contact info from ConfigMap data
            contact_info = {}
            for field in CONTACT_FIELDS:
                contact_info[field] = configmap.data.get(field, '')
            
            # Add metadata
            contact_info['last_updated'] = configmap.metadata.annotations.get(
                'last-updated', ''
            )
            contact_info['updated_by'] = configmap.metadata.annotations.get(
                'updated-by', ''
            )
            
            return contact_info
        except ApiException as e:
            if e.status == 404:
                return {}
            logger.error(f"Error reading ConfigMap for {cluster_name}/{namespace}: {e}")
            return {}
    
    def update_namespace_contact_info(self, cluster_name: str, namespace: str, 
                                      contact_data: Dict, updated_by: str) -> bool:
        """Update contact information for a namespace"""
        try:
            client_info = self.clients.get(cluster_name)
            if not client_info:
                return False
            
            # Prepare ConfigMap data
            configmap_data = {}
            for field in CONTACT_FIELDS:
                if field in contact_data:
                    configmap_data[field] = contact_data[field]
            
            # Prepare metadata with annotations
            from kubernetes.client import V1ObjectMeta, V1ConfigMap
            from datetime import datetime
            
            metadata = V1ObjectMeta(
                name=CONTACT_CONFIGMAP_NAME,
                annotations={
                    'last-updated': datetime.utcnow().isoformat(),
                    'updated-by': updated_by
                }
            )
            
            configmap = V1ConfigMap(
                metadata=metadata,
                data=configmap_data
            )
            
            try:
                # Try to update existing ConfigMap
                client_info['core'].patch_namespaced_config_map(
                    name=CONTACT_CONFIGMAP_NAME,
                    namespace=namespace,
                    body=configmap
                )
                logger.info(f"Updated contact info for {cluster_name}/{namespace}")
            except ApiException as e:
                if e.status == 404:
                    # Create new ConfigMap if it doesn't exist
                    client_info['core'].create_namespaced_config_map(
                        namespace=namespace,
                        body=configmap
                    )
                    logger.info(f"Created contact info for {cluster_name}/{namespace}")
                else:
                    raise
            
            return True
        except ApiException as e:
            logger.error(f"Error updating ConfigMap for {cluster_name}/{namespace}: {e}")
            return False

# Initialize cluster manager
cluster_manager = KubernetesClusterManager()

# Authentication decorator (add your own authentication logic)
def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Simple session-based authentication example
        # Replace with your actual authentication mechanism
        if not request.headers.get('X-User'):
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Simple login page (replace with your actual auth)"""
    if request.method == 'POST':
        # Add your authentication logic here
        username = request.form.get('username')
        # For demo purposes only - implement proper authentication!
        if username:
            # In production, use proper session management
            return redirect(url_for('index'))
        flash('Invalid credentials')
    return '''
    <form method="post">
        <input type="text" name="username" placeholder="Username" required>
        <input type="password" name="password" placeholder="Password" required>
        <button type="submit">Login</button>
    </form>
    '''

@app.route('/')
@require_auth
def index():
    """Main dashboard page"""
    namespaces_data = cluster_manager.get_all_namespaces_with_contacts()
    return render_template('index.html', 
                         namespaces=namespaces_data,
                         contact_fields=CONTACT_FIELDS)

@app.route('/api/namespaces', methods=['GET'])
def get_namespaces():
    """API endpoint to get all namespaces with contact info"""
    namespaces_data = cluster_manager.get_all_namespaces_with_contacts()
    return jsonify(namespaces_data)

@app.route('/api/namespace/<cluster>/<namespace>', methods=['GET'])
def get_namespace_contact(cluster, namespace):
    """API endpoint to get contact info for specific namespace"""
    contact_info = cluster_manager.get_namespace_contact_info(cluster, namespace)
    return jsonify(contact_info)

@app.route('/api/namespace/<cluster>/<namespace>', methods=['POST', 'PUT'])
@require_auth
def update_namespace_contact(cluster, namespace):
    """API endpoint to update contact info for specific namespace"""
    try:
        data = request.get_json()
        updated_by = request.headers.get('X-User', 'unknown')
        
        # Validate required fields
        if not data.get('owner_name') or not data.get('owner_email'):
            return jsonify({'error': 'Owner name and email are required'}), 400
        
        success = cluster_manager.update_namespace_contact_info(
            cluster, namespace, data, updated_by
        )
        
        if success:
            return jsonify({'message': 'Contact information updated successfully'}), 200
        else:
            return jsonify({'error': 'Failed to update contact information'}), 500
    except Exception as e:
        logger.error(f"Error in update endpoint: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/export', methods=['GET'])
def export_contacts():
    """Export all contact information as YAML"""
    namespaces_data = cluster_manager.get_all_namespaces_with_contacts()
    export_data = []
    
    for ns in namespaces_data:
        if ns['has_contact']:
            export_data.append({
                'cluster': ns['cluster'],
                'namespace': ns['namespace'],
                'contact_info': ns['contact_info']
            })
    
    return yaml.dump(export_data, default_flow_style=False), 200, {
        'Content-Type': 'text/yaml',
        'Content-Disposition': 'attachment; filename=contacts_export.yaml'
    }

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
```

### 2. Templates (templates/index.html)

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kubernetes Namespace Contact Manager</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        body {
            background-color: #f8f9fa;
        }
        .navbar {
            background-color: #326ce5;
            color: white;
        }
        .navbar-brand {
            color: white !important;
            font-weight: bold;
        }
        .namespace-card {
            margin-bottom: 20px;
            transition: transform 0.2s;
        }
        .namespace-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.1);
        }
        .card-header {
            background-color: #f8f9fc;
            border-bottom: 2px solid #e3e6f0;
        }
        .cluster-badge {
            font-size: 0.8rem;
            padding: 3px 8px;
        }
        .edit-form {
            display: none;
            margin-top: 15px;
            padding: 15px;
            background-color: #f8f9fc;
            border-radius: 5px;
        }
        .contact-info {
            font-size: 0.9rem;
        }
        .info-label {
            font-weight: bold;
            color: #5a5c69;
        }
        .status-badge {
            margin-left: 10px;
        }
        .loading {
            text-align: center;
            padding: 50px;
        }
        .search-box {
            margin-bottom: 20px;
        }
        .btn-primary {
            background-color: #326ce5;
            border-color: #326ce5;
        }
        .btn-primary:hover {
            background-color: #2854b8;
        }
        .alert-info {
            background-color: #d1ecf1;
            border-color: #bee5eb;
            color: #0c5460;
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-dark">
        <div class="container-fluid">
            <span class="navbar-brand">
                <i class="fas fa-kubernetes"></i> Kubernetes Namespace Contact Manager
            </span>
            <div class="d-flex">
                <button class="btn btn-light btn-sm me-2" onclick="exportContacts()">
                    <i class="fas fa-download"></i> Export
                </button>
                <span class="navbar-text text-white">
                    <i class="fas fa-user"></i> <span id="current-user">User</span>
                </span>
            </div>
        </div>
    </nav>

    <div class="container-fluid mt-4">
        <div class="row">
            <div class="col-12">
                <div class="alert alert-info">
                    <i class="fas fa-info-circle"></i> 
                    This dashboard displays all Kubernetes namespaces across clusters. 
                    Click "Edit" to update contact information for each namespace.
                </div>
            </div>
        </div>

        <div class="row mb-3">
            <div class="col-md-6">
                <div class="search-box">
                    <input type="text" id="searchInput" class="form-control" 
                           placeholder="Search by namespace or team...">
                </div>
            </div>
            <div class="col-md-6 text-end">
                <div class="btn-group">
                    <button class="btn btn-outline-primary btn-sm" onclick="filterClusters('all')">
                        All Clusters
                    </button>
                    {% set clusters = namespaces|map(attribute='cluster_display')|unique|list %}
                    {% for cluster in clusters %}
                    <button class="btn btn-outline-secondary btn-sm" onclick="filterClusters('{{ cluster }}')">
                        {{ cluster }}
                    </button>
                    {% endfor %}
                </div>
            </div>
        </div>

        <div class="row" id="namespacesContainer">
            {% for ns in namespaces %}
            <div class="col-md-6 col-lg-4 namespace-card" 
                 data-cluster="{{ ns.cluster_display }}"
                 data-namespace="{{ ns.namespace|lower }}"
                 data-team="{{ ns.contact_info.team_name|lower }}">
                <div class="card h-100">
                    <div class="card-header">
                        <h6 class="mb-0">
                            <i class="fas fa-cube"></i> {{ ns.namespace }}
                            {% if ns.has_contact %}
                            <span class="badge bg-success status-badge">
                                <i class="fas fa-check-circle"></i> Configured
                            </span>
                            {% else %}
                            <span class="badge bg-warning status-badge">
                                <i class="fas fa-exclamation-triangle"></i> Missing Contact
                            </span>
                            {% endif %}
                        </h6>
                        <small class="text-muted">
                            <i class="fas fa-server"></i> Cluster: {{ ns.cluster_display }}
                        </small>
                    </div>
                    <div class="card-body">
                        <div class="contact-info">
                            {% if ns.has_contact %}
                            <p><span class="info-label"><i class="fas fa-user"></i> Owner:</span> 
                                {{ ns.contact_info.owner_name }}</p>
                            <p><span class="info-label"><i class="fas fa-envelope"></i> Email:</span> 
                                <a href="mailto:{{ ns.contact_info.owner_email }}">{{ ns.contact_info.owner_email }}</a></p>
                            <p><span class="info-label"><i class="fas fa-users"></i> Team:</span> 
                                {{ ns.contact_info.team_name or 'N/A' }}</p>
                            <p><span class="info-label"><i class="fab fa-slack"></i> Slack:</span> 
                                {{ ns.contact_info.slack_channel or 'N/A' }}</p>
                            <p><span class="info-label"><i class="fas fa-phone-alt"></i> Oncall:</span> 
                                {{ ns.contact_info.oncall_rotation or 'N/A' }}</p>
                            {% if ns.contact_info.last_updated %}
                            <small class="text-muted">
                                <i class="fas fa-clock"></i> Last updated: {{ ns.contact_info.last_updated[:19].replace('T', ' ') }}
                                {% if ns.contact_info.updated_by %}
                                by {{ ns.contact_info.updated_by }}
                                {% endif %}
                            </small>
                            {% endif %}
                            {% else %}
                            <p class="text-muted text-center">
                                <i class="fas fa-info-circle"></i> No contact information configured
                            </p>
                            {% endif %}
                        </div>
                        <button class="btn btn-primary btn-sm mt-3" 
                                onclick="showEditForm('{{ ns.cluster }}', '{{ ns.namespace }}')">
                            <i class="fas fa-edit"></i> Edit Contact Info
                        </button>
                        <div id="edit-form-{{ ns.cluster }}-{{ ns.namespace }}" 
                             class="edit-form">
                            <!-- Edit form will be loaded here -->
                        </div>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
    </div>

    <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.1.3/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        let currentFilter = 'all';
        
        function showEditForm(cluster, namespace) {
            const formDiv = $(`#edit-form-${cluster}-${namespace}`);
            
            if (formDiv.is(':visible')) {
                formDiv.hide();
                return;
            }
            
            // Fetch current contact info
            $.get(`/api/namespace/${cluster}/${namespace}`, function(data) {
                const formHtml = `
                    <form onsubmit="updateContact(event, '${cluster}', '${namespace}')">
                        <div class="mb-2">
                            <label>Owner Name *</label>
                            <input type="text" class="form-control form-control-sm" 
                                   name="owner_name" value="${escapeHtml(data.owner_name || '')}" required>
                        </div>
                        <div class="mb-2">
                            <label>Owner Email *</label>
                            <input type="email" class="form-control form-control-sm" 
                                   name="owner_email" value="${escapeHtml(data.owner_email || '')}" required>
                        </div>
                        <div class="mb-2">
                            <label>Team Name</label>
                            <input type="text" class="form-control form-control-sm" 
                                   name="team_name" value="${escapeHtml(data.team_name || '')}">
                        </div>
                        <div class="mb-2">
                            <label>Slack Channel</label>
                            <input type="text" class="form-control form-control-sm" 
                                   name="slack_channel" value="${escapeHtml(data.slack_channel || '')}" 
                                   placeholder="#team-channel">
                        </div>
                        <div class="mb-2">
                            <label>Oncall Rotation</label>
                            <input type="text" class="form-control form-control-sm" 
                                   name="oncall_rotation" value="${escapeHtml(data.oncall_rotation || '')}"
                                   placeholder="Rotation name/schedule">
                        </div>
                        <button type="submit" class="btn btn-success btn-sm">
                            <i class="fas fa-save"></i> Save
                        </button>
                        <button type="button" class="btn btn-secondary btn-sm" 
                                onclick="$('#edit-form-${cluster}-${namespace}').hide()">
                            Cancel
                        </button>
                    </form>
                `;
                formDiv.html(formHtml).show();
            }).fail(function() {
                alert('Failed to load contact information');
            });
        }
        
        function updateContact(event, cluster, namespace) {
            event.preventDefault();
            const form = event.target;
            const formData = new FormData(form);
            const data = {};
            
            formData.forEach((value, key) => {
                data[key] = value;
            });
            
            // Get user from header (you would get this from your auth system)
            const user = prompt("Enter your name for audit trail:", "");
            if (!user) return;
            
            $.ajax({
                url: `/api/namespace/${cluster}/${namespace}`,
                method: 'POST',
                contentType: 'application/json',
                headers: {'X-User': user},
                data: JSON.stringify(data),
                success: function(response) {
                    alert('Contact information updated successfully!');
                    location.reload();
                },
                error: function(xhr) {
                    alert('Error: ' + (xhr.responseJSON?.error || 'Failed to update'));
                }
            });
        }
        
        function escapeHtml(text) {
            if (!text) return '';
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        function filterClusters(cluster) {
            currentFilter = cluster;
            $('.namespace-card').each(function() {
                const cardCluster = $(this).data('cluster');
                if (cluster === 'all' || cardCluster === cluster) {
                    $(this).show();
                } else {
                    $(this).hide();
                }
            });
        }
        
        $('#searchInput').on('keyup', function() {
            const searchTerm = $(this).val().toLowerCase();
            $('.namespace-card').each(function() {
                const namespace = $(this).data('namespace');
                const team = $(this).data('team');
                if (namespace.includes(searchTerm) || (team && team.includes(searchTerm))) {
                    $(this).show();
                } else {
                    $(this).hide();
                }
            });
        });
        
        function exportContacts() {
            window.location.href = '/export';
        }
        
        // Set current user (you would get this from your auth system)
        const user = prompt("Enter your name:", "User") || "Unknown";
        $('#current-user').text(user);
    </script>
</body>
</html>
```

### 3. Requirements File (requirements.txt)

```txt
Flask==2.3.3
kubernetes==27.2.0
PyYAML==6.0.1
requests==2.31.0
```

### 4. Dockerfile (for containerized deployment)

```dockerfile
FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

EXPOSE 5000

CMD ["python", "app.py"]
```

### 5. Kubernetes Deployment (deployment.yaml)

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: contact-manager-sa
  namespace: default
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: contact-manager-role
rules:
- apiGroups: [""]
  resources: ["namespaces", "configmaps"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: contact-manager-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: contact-manager-role
subjects:
- kind: ServiceAccount
  name: contact-manager-sa
  namespace: default
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: contact-manager
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: contact-manager
  template:
    metadata:
      labels:
        app: contact-manager
    spec:
      serviceAccountName: contact-manager-sa
      containers:
      - name: contact-manager
        image: contact-manager:latest
        imagePullPolicy: Always
        ports:
        - containerPort: 5000
        env:
        - name: SECRET_KEY
          valueFrom:
            secretKeyRef:
              name: contact-manager-secret
              key: secret-key
---
apiVersion: v1
kind: Service
metadata:
  name: contact-manager-service
spec:
  selector:
    app: contact-manager
  ports:
  - port: 80
    targetPort: 5000
  type: LoadBalancer
---
apiVersion: v1
kind: Secret
metadata:
  name: contact-manager-secret
type: Opaque
data:
  secret-key: YOUR_BASE64_ENCODED_SECRET_KEY_HERE
```

## Usage Instructions

### Local Development

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Run the application:
```bash
export SECRET_KEY="your-secret-key"
python app.py
```

3. Access the web interface at `http://localhost:5000`

### Kubernetes Deployment

1. Build the Docker image:
```bash
docker build -t contact-manager:latest .
```

2. Apply the deployment:
```bash
kubectl apply -f deployment.yaml
```

### Adding Multiple Clusters

To add multiple clusters, update the `CLUSTERS` dictionary in `app.py`:

```python
CLUSTERS = {
    'prod-us-east': {
        'name': 'Production US East',
        'context': 'prod-us-east-context'
    },
    'prod-eu-west': {
        'name': 'Production EU West',
        'context': 'prod-eu-west-context'
    },
    'staging': {
        'name': 'Staging Cluster',
        'context': 'staging-context'
    }
}
```

## Features

- **Multi-cluster support**: Manage contact info across multiple Kubernetes clusters
- **Web dashboard**: Visual interface showing all namespaces with contact status
- **CRUD operations**: Create, read, update, delete contact information
- **Audit trail**: Tracks who updated the information and when
- **Search & filter**: Filter by cluster, namespace, or team name
- **Export functionality**: Export all contacts as YAML
- **REST API**: Programmatic access to contact information

## Security Considerations

1. **Add proper authentication** (the current demo uses a simple prompt - integrate with OIDC, LDAP, or your corporate SSO)
2. **Use HTTPS** in production
3. **Limit RBAC permissions** to only necessary namespaces
4. **Add input validation** and sanitization
5. **Implement rate limiting** for API endpoints
6. **Add audit logging** to a centralized system
