from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session, g
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from functools import wraps
import logging
from typing import Dict, List, Optional
from datetime import datetime
import os
import json
from sqlalchemy import or_, and_
from sqlalchemy.exc import IntegrityError

from models import db, Cluster, NamespaceContact, ContactHistory, ContactAuditLog, SyncStatus

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-here')

# Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'postgresql://user:password@localhost/contact_manager'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'pool_recycle': 3600,
    'pool_pre_ping': True,
}

# Initialize database
db.init_app(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load Kubernetes configuration
def load_k8s_config():
    """Load Kubernetes configuration based on environment"""
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes configuration")
    except config.ConfigException:
        try:
            config.load_kube_config()
            logger.info("Loaded kubeconfig file")
        except config.ConfigException as e:
            logger.error(f"Could not load Kubernetes configuration: {e}")
            raise

load_k8s_config()

# Initialize Kubernetes API clients
core_v1_api = client.CoreV1Api()

# Name of the ConfigMap that will store contact information
CONTACT_CONFIGMAP_NAME = "namespace-contact-info"

# Required contact fields
CONTACT_FIELDS = [
    'owner_name', 'owner_email', 'team_name', 'slack_channel', 
    'oncall_rotation', 'backup_owner_name', 'backup_owner_email',
    'jira_project', 'confluence_space', 'additional_notes'
]

class EnhancedClusterManager:
    """Enhanced manager with database integration"""
    
    def __init__(self):
        self.k8s_clients = {}
        self._init_kubernetes_clients()
    
    def _init_kubernetes_clients(self):
        """Initialize Kubernetes clients from database clusters"""
        try:
            clusters = Cluster.query.filter_by(is_active=True).all()
            for cluster in clusters:
                try:
                    if cluster.context:
                        config.load_kube_config(context=cluster.context)
                    else:
                        config.load_kube_config()
                    
                    self.k8s_clients[cluster.name] = {
                        'core': client.CoreV1Api(),
                        'id': cluster.id,
                        'display_name': cluster.display_name
                    }
                    logger.info(f"Initialized K8s client for cluster: {cluster.name}")
                except Exception as e:
                    logger.error(f"Failed to initialize K8s client for {cluster.name}: {e}")
        except Exception as e:
            logger.error(f"Error loading clusters from database: {e}")
    
    def sync_namespace_from_k8s(self, cluster_name: str, namespace: str, updated_by: str = "system"):
        """Sync a single namespace from Kubernetes to database"""
        try:
            k8s_client = self.k8s_clients.get(cluster_name)
            if not k8s_client:
                return False
            
            cluster = Cluster.query.filter_by(name=cluster_name).first()
            if not cluster:
                return False
            
            # Get ConfigMap from Kubernetes
            try:
                configmap = k8s_client['core'].read_namespaced_config_map(
                    name=CONTACT_CONFIGMAP_NAME,
                    namespace=namespace
                )
                
                # Extract contact info from ConfigMap
                contact_data = {}
                for field in CONTACT_FIELDS:
                    contact_data[field] = configmap.data.get(field, '')
                
                # Update or create in database
                contact = NamespaceContact.query.filter_by(
                    cluster_id=cluster.id,
                    namespace=namespace
                ).first()
                
                if contact:
                    # Store old data for history
                    old_data = contact.to_dict()
                    
                    # Update existing record
                    for field in CONTACT_FIELDS:
                        setattr(contact, field, contact_data.get(field, ''))
                    
                    contact.last_updated_by = updated_by
                    contact.updated_at = datetime.utcnow()
                    contact.version += 1
                    
                    # Add to history
                    history = ContactHistory(
                        contact_id=contact.id,
                        cluster_id=cluster.id,
                        namespace=namespace,
                        old_data=old_data,
                        new_data=contact.to_dict(),
                        change_type='SYNC',
                        changed_by=updated_by
                    )
                    db.session.add(history)
                else:
                    # Create new contact
                    contact = NamespaceContact(
                        cluster_id=cluster.id,
                        namespace=namespace,
                        **contact_data,
                        last_updated_by=updated_by,
                        sync_with_k8s=True
                    )
                    db.session.add(contact)
                    db.session.flush()
                    
                    # Add to history
                    history = ContactHistory(
                        contact_id=contact.id,
                        cluster_id=cluster.id,
                        namespace=namespace,
                        old_data={},
                        new_data=contact.to_dict(),
                        change_type='CREATE',
                        changed_by=updated_by
                    )
                    db.session.add(history)
                
                db.session.commit()
                logger.info(f"Synced {cluster_name}/{namespace} from Kubernetes")
                return True
                
            except ApiException as e:
                if e.status == 404:
                    # No ConfigMap exists, check if we have a DB record
                    contact = NamespaceContact.query.filter_by(
                        cluster_id=cluster.id,
                        namespace=namespace
                    ).first()
                    
                    if contact:
                        # Remove or mark as not synced
                        contact.sync_with_k8s = False
                        db.session.commit()
                    
                    logger.info(f"No ConfigMap found for {cluster_name}/{namespace}")
                else:
                    raise
                return False
                
        except Exception as e:
            logger.error(f"Error syncing {cluster_name}/{namespace}: {e}")
            db.session.rollback()
            return False
    
    def push_to_kubernetes(self, cluster_name: str, namespace: str, contact_id: int, updated_by: str) -> bool:
        """Push contact information from database to Kubernetes ConfigMap"""
        try:
            k8s_client = self.k8s_clients.get(cluster_name)
            if not k8s_client:
                return False
            
            contact = NamespaceContact.query.get(contact_id)
            if not contact:
                return False
            
            # Prepare ConfigMap data
            configmap_data = {}
            for field in CONTACT_FIELDS:
                value = getattr(contact, field, '')
                if value:
                    configmap_data[field] = value
            
            # Prepare metadata with annotations
            from kubernetes.client import V1ObjectMeta, V1ConfigMap
            
            metadata = V1ObjectMeta(
                name=CONTACT_CONFIGMAP_NAME,
                annotations={
                    'last-updated': datetime.utcnow().isoformat(),
                    'updated-by': updated_by,
                    'database-id': str(contact.id),
                    'version': str(contact.version)
                }
            )
            
            configmap = V1ConfigMap(
                metadata=metadata,
                data=configmap_data
            )
            
            try:
                # Try to update existing ConfigMap
                k8s_client['core'].patch_namespaced_config_map(
                    name=CONTACT_CONFIGMAP_NAME,
                    namespace=namespace,
                    body=configmap
                )
                logger.info(f"Updated ConfigMap for {cluster_name}/{namespace}")
            except ApiException as e:
                if e.status == 404:
                    # Create new ConfigMap
                    k8s_client['core'].create_namespaced_config_map(
                        namespace=namespace,
                        body=configmap
                    )
                    logger.info(f"Created ConfigMap for {cluster_name}/{namespace}")
                else:
                    raise
            
            # Update sync status
            contact.sync_with_k8s = True
            contact.last_updated_by = updated_by
            db.session.commit()
            
            return True
            
        except Exception as e:
            logger.error(f"Error pushing to Kubernetes {cluster_name}/{namespace}: {e}")
            return False
    
    def get_all_namespaces_with_contacts(self, search: str = None, cluster_id: int = None, 
                                        has_contact_only: bool = False) -> List[Dict]:
        """Get all namespaces with contact information from database"""
        try:
            query = NamespaceContact.query.join(Cluster).filter(Cluster.is_active == True)
            
            # Apply filters
            if cluster_id:
                query = query.filter(NamespaceContact.cluster_id == cluster_id)
            
            if has_contact_only:
                query = query.filter(NamespaceContact.owner_name.isnot(None))
                query = query.filter(NamespaceContact.owner_email.isnot(None))
            
            if search:
                search_term = f"%{search}%"
                query = query.filter(
                    or_(
                        NamespaceContact.namespace.ilike(search_term),
                        NamespaceContact.owner_name.ilike(search_term),
                        NamespaceContact.team_name.ilike(search_term),
                        NamespaceContact.owner_email.ilike(search_term)
                    )
                )
            
            contacts = query.all()
            
            result = []
            for contact in contacts:
                contact_dict = contact.to_dict()
                contact_dict['has_contact'] = bool(contact.owner_name and contact.owner_email)
                result.append(contact_dict)
            
            return result
            
        except Exception as e:
            logger.error(f"Error getting namespaces from database: {e}")
            return []
    
    def get_namespace_contact(self, cluster_name: str, namespace: str) -> Optional[Dict]:
        """Get contact information for a specific namespace from database"""
        try:
            cluster = Cluster.query.filter_by(name=cluster_name).first()
            if not cluster:
                return None
            
            contact = NamespaceContact.query.filter_by(
                cluster_id=cluster.id,
                namespace=namespace
            ).first()
            
            if contact:
                return contact.to_dict()
            return None
            
        except Exception as e:
            logger.error(f"Error getting namespace contact: {e}")
            return None
    
    def update_namespace_contact(self, cluster_name: str, namespace: str, 
                                contact_data: Dict, updated_by: str, 
                                ip_address: str = None, user_agent: str = None) -> Dict:
        """Update contact information in database"""
        try:
            cluster = Cluster.query.filter_by(name=cluster_name).first()
            if not cluster:
                return {'success': False, 'error': 'Cluster not found'}
            
            contact = NamespaceContact.query.filter_by(
                cluster_id=cluster.id,
                namespace=namespace
            ).first()
            
            # Store old data for history and audit
            old_data = None
            change_type = 'UPDATE'
            
            if contact:
                old_data = contact.to_dict()
                # Update existing record
                for field in CONTACT_FIELDS:
                    if field in contact_data:
                        setattr(contact, field, contact_data.get(field, ''))
                
                contact.last_updated_by = updated_by
                contact.updated_at = datetime.utcnow()
                contact.version += 1
                
                # Optionally sync to Kubernetes
                sync_to_k8s = contact_data.get('sync_to_k8s', True)
                if sync_to_k8s:
                    contact.sync_with_k8s = True
                
            else:
                change_type = 'CREATE'
                # Create new record
                contact = NamespaceContact(
                    cluster_id=cluster.id,
                    namespace=namespace,
                    **{field: contact_data.get(field, '') for field in CONTACT_FIELDS},
                    last_updated_by=updated_by,
                    sync_with_k8s=contact_data.get('sync_to_k8s', True)
                )
                db.session.add(contact)
                db.session.flush()
            
            # Add history entry
            history = ContactHistory(
                contact_id=contact.id,
                cluster_id=cluster.id,
                namespace=namespace,
                old_data=old_data,
                new_data=contact.to_dict(),
                change_type=change_type,
                changed_by=updated_by
            )
            db.session.add(history)
            
            # Add audit log
            audit = ContactAuditLog(
                action=change_type,
                cluster_name=cluster_name,
                namespace=namespace,
                performed_by=updated_by,
                ip_address=ip_address,
                user_agent=user_agent,
                details={'contact_data': contact_data, 'version': contact.version}
            )
            db.session.add(audit)
            
            db.session.commit()
            
            # Push to Kubernetes if requested
            if contact.sync_with_k8s:
                push_success = self.push_to_kubernetes(cluster_name, namespace, contact.id, updated_by)
                if not push_success:
                    logger.warning(f"Failed to push to Kubernetes for {cluster_name}/{namespace}")
            
            return {'success': True, 'contact': contact.to_dict(), 'change_type': change_type}
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error updating namespace contact: {e}")
            return {'success': False, 'error': str(e)}
    
    def delete_namespace_contact(self, cluster_name: str, namespace: str, 
                                updated_by: str, ip_address: str = None, 
                                user_agent: str = None) -> Dict:
        """Delete contact information from database (soft delete by clearing data)"""
        try:
            cluster = Cluster.query.filter_by(name=cluster_name).first()
            if not cluster:
                return {'success': False, 'error': 'Cluster not found'}
            
            contact = NamespaceContact.query.filter_by(
                cluster_id=cluster.id,
                namespace=namespace
            ).first()
            
            if not contact:
                return {'success': False, 'error': 'Contact not found'}
            
            # Store old data for history
            old_data = contact.to_dict()
            
            # Clear contact fields (soft delete)
            for field in CONTACT_FIELDS:
                setattr(contact, field, '')
            
            contact.last_updated_by = updated_by
            contact.updated_at = datetime.utcnow()
            contact.version += 1
            contact.sync_with_k8s = False
            
            # Add history entry
            history = ContactHistory(
                contact_id=contact.id,
                cluster_id=cluster.id,
                namespace=namespace,
                old_data=old_data,
                new_data=contact.to_dict(),
                change_type='DELETE',
                changed_by=updated_by
            )
            db.session.add(history)
            
            # Add audit log
            audit = ContactAuditLog(
                action='DELETE',
                cluster_name=cluster_name,
                namespace=namespace,
                performed_by=updated_by,
                ip_address=ip_address,
                user_agent=user_agent,
                details={'version': contact.version}
            )
            db.session.add(audit)
            
            db.session.commit()
            
            return {'success': True}
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error deleting namespace contact: {e}")
            return {'success': False, 'error': str(e)}
    
    def bulk_sync_all_namespaces(self, updated_by: str = "system") -> Dict:
        """Sync all namespaces from all clusters"""
        results = {'success': 0, 'failed': 0, 'total': 0}
        
        for cluster_name, k8s_client in self.k8s_clients.items():
            try:
                namespaces = k8s_client['core'].list_namespace()
                
                for ns in namespaces.items:
                    namespace_name = ns.metadata.name
                    if self.sync_namespace_from_k8s(cluster_name, namespace_name, updated_by):
                        results['success'] += 1
                    else:
                        results['failed'] += 1
                    results['total'] += 1
                    
            except Exception as e:
                logger.error(f"Error syncing cluster {cluster_name}: {e}")
                results['failed'] += 1
        
        return results
    
    def get_contact_history(self, cluster_name: str = None, namespace: str = None, 
                           limit: int = 100) -> List[Dict]:
        """Get history of changes"""
        try:
            query = ContactHistory.query.join(Cluster)
            
            if cluster_name:
                query = query.filter(Cluster.name == cluster_name)
            if namespace:
                query = query.filter(ContactHistory.namespace == namespace)
            
            history = query.order_by(ContactHistory.changed_at.desc()).limit(limit).all()
            return [h.to_dict() for h in history]
            
        except Exception as e:
            logger.error(f"Error getting history: {e}")
            return []
    
    def get_audit_logs(self, limit: int = 100) -> List[Dict]:
        """Get audit logs"""
        try:
            logs = ContactAuditLog.query.order_by(
                ContactAuditLog.created_at.desc()
            ).limit(limit).all()
            return [log.to_dict() for log in logs]
            
        except Exception as e:
            logger.error(f"Error getting audit logs: {e}")
            return []

# Initialize manager
cluster_manager = EnhancedClusterManager()

# Authentication decorator
def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Get user from session or header
        user = session.get('username') or request.headers.get('X-User')
        if not user:
            if request.is_json:
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        
        g.current_user = user
        g.ip_address = request.remote_addr
        g.user_agent = request.headers.get('User-Agent')
        
        return f(*args, **kwargs)
    return decorated_function

# Initialize database
@app.before_first_request
def create_tables():
    db.create_all()
    # Create default clusters if none exist
    if Cluster.query.count() == 0:
        default_clusters = [
            Cluster(name='default', display_name='Default Cluster', is_active=True),
            # Add more clusters as needed
        ]
        for cluster in default_clusters:
            db.session.add(cluster)
        db.session.commit()
        cluster_manager._init_kubernetes_clients()

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # Add your authentication logic here
        # This is just a demo - implement proper authentication!
        if username and password:  # Add real validation
            session['username'] = username
            return redirect(url_for('index'))
        flash('Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('login'))

@app.route('/')
@require_auth
def index():
    """Main dashboard page"""
    clusters = Cluster.query.filter_by(is_active=True).all()
    return render_template('dashboard.html', 
                         clusters=clusters,
                         contact_fields=CONTACT_FIELDS)

# API Endpoints

@app.route('/api/namespaces', methods=['GET'])
@require_auth
def get_namespaces():
    """Get all namespaces with contact info"""
    search = request.args.get('search')
    cluster_id = request.args.get('cluster_id', type=int)
    has_contact_only = request.args.get('has_contact_only', 'false').lower() == 'true'
    
    namespaces = cluster_manager.get_all_namespaces_with_contacts(
        search=search,
        cluster_id=cluster_id,
        has_contact_only=has_contact_only
    )
    return jsonify(namespaces)

@app.route('/api/namespace/<cluster_name>/<namespace>', methods=['GET'])
@require_auth
def get_namespace_contact(cluster_name, namespace):
    """Get contact info for specific namespace"""
    contact_info = cluster_manager.get_namespace_contact(cluster_name, namespace)
    if contact_info:
        return jsonify(contact_info)
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/namespace/<cluster_name>/<namespace>', methods=['POST', 'PUT'])
@require_auth
def update_namespace_contact(cluster_name, namespace):
    """Update contact info for specific namespace"""
    try:
        data = request.get_json()
        
        # Validate required fields
        if not data.get('owner_name') or not data.get('owner_email'):
            return jsonify({'error': 'Owner name and email are required'}), 400
        
        result = cluster_manager.update_namespace_contact(
            cluster_name=cluster_name,
            namespace=namespace,
            contact_data=data,
            updated_by=g.current_user,
            ip_address=g.ip_address,
            user_agent=g.user_agent
        )
        
        if result['success']:
            return jsonify(result), 200
        else:
            return jsonify({'error': result.get('error', 'Update failed')}), 500
            
    except Exception as e:
        logger.error(f"Error in update endpoint: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/namespace/<cluster_name>/<namespace>', methods=['DELETE'])
@require_auth
def delete_namespace_contact(cluster_name, namespace):
    """Delete contact info (clear data)"""
    result = cluster_manager.delete_namespace_contact(
        cluster_name=cluster_name,
        namespace=namespace,
        updated_by=g.current_user,
        ip_address=g.ip_address,
        user_agent=g.user_agent
    )
    
    if result['success']:
        return jsonify({'message': 'Contact information deleted'}), 200
    else:
        return jsonify({'error': result.get('error', 'Delete failed')}), 500

@app.route('/api/sync/namespace/<cluster_name>/<namespace>', methods=['POST'])
@require_auth
def sync_namespace(cluster_name, namespace):
    """Sync a single namespace from Kubernetes"""
    success = cluster_manager.sync_namespace_from_k8s(
        cluster_name, namespace, g.current_user
    )
    if success:
        return jsonify({'message': 'Sync completed'}), 200
    else:
        return jsonify({'error': 'Sync failed'}), 500

@app.route('/api/sync/bulk', methods=['POST'])
@require_auth
def bulk_sync():
    """Sync all namespaces from all clusters"""
    results = cluster_manager.bulk_sync_all_namespaces(g.current_user)
    return jsonify(results), 200

@app.route('/api/history', methods=['GET'])
@require_auth
def get_history():
    """Get contact history"""
    cluster_name = request.args.get('cluster')
    namespace = request.args.get('namespace')
    limit = request.args.get('limit', 100, type=int)
    
    history = cluster_manager.get_contact_history(cluster_name, namespace, limit)
    return jsonify(history)

@app.route('/api/audit', methods=['GET'])
@require_auth
def get_audit():
    """Get audit logs"""
    limit = request.args.get('limit', 100, type=int)
    logs = cluster_manager.get_audit_logs(limit)
    return jsonify(logs)

@app.route('/api/clusters', methods=['GET'])
@require_auth
def get_clusters():
    """Get all clusters"""
    clusters = Cluster.query.filter_by(is_active=True).all()
    return jsonify([c.to_dict() for c in clusters])

@app.route('/api/clusters', methods=['POST'])
@require_auth
def add_cluster():
    """Add a new cluster"""
    data = request.get_json()
    
    cluster = Cluster(
        name=data['name'],
        display_name=data['display_name'],
        context=data.get('context'),
        api_endpoint=data.get('api_endpoint')
    )
    
    try:
        db.session.add(cluster)
        db.session.commit()
        cluster_manager._init_kubernetes_clients()
        return jsonify(cluster.to_dict()), 201
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'Cluster name already exists'}), 400

@app.route('/api/export', methods=['GET'])
@require_auth
def export_contacts():
    """Export all contacts as JSON"""
    namespaces = cluster_manager.get_all_namespaces_with_contacts()
    
    from flask import Response
    import json
    
    return Response(
        json.dumps(namespaces, indent=2),
        mimetype='application/json',
        headers={'Content-Disposition': 'attachment;filename=contacts_export.json'}
    )

@app.route('/api/import', methods=['POST'])
@require_auth
def import_contacts():
    """Import contacts from JSON"""
    data = request.get_json()
    
    if not isinstance(data, list):
        return jsonify({'error': 'Invalid format, expected array'}), 400
    
    results = {'success': 0, 'failed': 0}
    
    for item in data:
        cluster_name = item.get('cluster_name')
        namespace = item.get('namespace')
        contact_data = item.get('contact_info', {})
        
        if cluster_name and namespace:
            result = cluster_manager.update_namespace_contact(
                cluster_name=cluster_name,
                namespace=namespace,
                contact_data=contact_data,
                updated_by=g.current_user,
                ip_address=g.ip_address,
                user_agent=g.user_agent
            )
            
            if result['success']:
                results['success'] += 1
            else:
                results['failed'] += 1
    
    return jsonify(results), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
