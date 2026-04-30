from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from sqlalchemy import Index, UniqueConstraint
import json

db = SQLAlchemy()

class Cluster(db.Model):
    """Model for storing cluster information"""
    __tablename__ = 'clusters'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    display_name = db.Column(db.String(255), nullable=False)
    context = db.Column(db.String(255))
    api_endpoint = db.Column(db.String(500))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    namespace_contacts = db.relationship('NamespaceContact', backref='cluster', lazy='dynamic')
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'context': self.context,
            'api_endpoint': self.api_endpoint,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

class NamespaceContact(db.Model):
    """Model for storing namespace contact information"""
    __tablename__ = 'namespace_contacts'
    __table_args__ = (
        UniqueConstraint('cluster_id', 'namespace', name='uq_cluster_namespace'),
        Index('idx_cluster_namespace', 'cluster_id', 'namespace'),
        Index('idx_owner_email', 'owner_email'),
        Index('idx_team_name', 'team_name'),
        Index('idx_updated_at', 'updated_at'),
    )
    
    id = db.Column(db.Integer, primary_key=True)
    cluster_id = db.Column(db.Integer, db.ForeignKey('clusters.id', ondelete='CASCADE'), nullable=False)
    namespace = db.Column(db.String(255), nullable=False)
    
    # Contact information fields
    owner_name = db.Column(db.String(255), nullable=False)
    owner_email = db.Column(db.String(255), nullable=False)
    team_name = db.Column(db.String(255))
    slack_channel = db.Column(db.String(255))
    oncall_rotation = db.Column(db.String(255))
    backup_owner_name = db.Column(db.String(255))
    backup_owner_email = db.Column(db.String(255))
    jira_project = db.Column(db.String(255))
    confluence_space = db.Column(db.String(255))
    additional_notes = db.Column(db.Text)
    
    # Custom fields (JSON for flexibility)
    custom_fields = db.Column(db.JSON, default={})
    
    # Metadata
    last_updated_by = db.Column(db.String(255))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    version = db.Column(db.Integer, default=1)
    sync_with_k8s = db.Column(db.Boolean, default=True)
    
    def to_dict(self):
        return {
            'id': self.id,
            'cluster_id': self.cluster_id,
            'cluster_name': self.cluster.name if self.cluster else None,
            'namespace': self.namespace,
            'owner_name': self.owner_name,
            'owner_email': self.owner_email,
            'team_name': self.team_name,
            'slack_channel': self.slack_channel,
            'oncall_rotation': self.oncall_rotation,
            'backup_owner_name': self.backup_owner_name,
            'backup_owner_email': self.backup_owner_email,
            'jira_project': self.jira_project,
            'confluence_space': self.confluence_space,
            'additional_notes': self.additional_notes,
            'custom_fields': self.custom_fields,
            'last_updated_by': self.last_updated_by,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'version': self.version,
            'sync_with_k8s': self.sync_with_k8s
        }

class ContactHistory(db.Model):
    """Model for tracking historical changes"""
    __tablename__ = 'contact_history'
    
    id = db.Column(db.Integer, primary_key=True)
    contact_id = db.Column(db.Integer, db.ForeignKey('namespace_contacts.id', ondelete='CASCADE'), nullable=False)
    cluster_id = db.Column(db.Integer, db.ForeignKey('clusters.id'), nullable=False)
    namespace = db.Column(db.String(255), nullable=False)
    
    # Snapshot of the contact data before change
    old_data = db.Column(db.JSON)
    new_data = db.Column(db.JSON)
    
    change_type = db.Column(db.String(50))  # CREATE, UPDATE, DELETE, SYNC
    changed_by = db.Column(db.String(255))
    changed_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    def to_dict(self):
        return {
            'id': self.id,
            'contact_id': self.contact_id,
            'cluster_id': self.cluster_id,
            'namespace': self.namespace,
            'old_data': self.old_data,
            'new_data': self.new_data,
            'change_type': self.change_type,
            'changed_by': self.changed_by,
            'changed_at': self.changed_at.isoformat() if self.changed_at else None
        }

class ContactAuditLog(db.Model):
    """Model for audit logging"""
    __tablename__ = 'contact_audit_log'
    
    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(50), nullable=False)
    cluster_name = db.Column(db.String(255))
    namespace = db.Column(db.String(255))
    performed_by = db.Column(db.String(255), nullable=False)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(500))
    details = db.Column(db.JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    def to_dict(self):
        return {
            'id': self.id,
            'action': self.action,
            'cluster_name': self.cluster_name,
            'namespace': self.namespace,
            'performed_by': self.performed_by,
            'ip_address': self.ip_address,
            'user_agent': self.user_agent,
            'details': self.details,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

class SyncStatus(db.Model):
    """Model for tracking sync status with Kubernetes"""
    __tablename__ = 'sync_status'
    
    id = db.Column(db.Integer, primary_key=True)
    cluster_id = db.Column(db.Integer, db.ForeignKey('clusters.id'), nullable=False)
    last_sync_at = db.Column(db.DateTime)
    sync_status = db.Column(db.String(50))  # SUCCESS, FAILED, IN_PROGRESS
    error_message = db.Column(db.Text)
    namespaces_synced = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'cluster_id': self.cluster_id,
            'cluster_name': self.cluster.name if self.cluster else None,
            'last_sync_at': self.last_sync_at.isoformat() if self.last_sync_at else None,
            'sync_status': self.sync_status,
            'error_message': self.error_message,
            'namespaces_synced': self.namespaces_synced,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
