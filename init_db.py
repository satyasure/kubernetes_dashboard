from app import app, db
from models import Cluster, NamespaceContact

def init_database():
    with app.app_context():
        # Create all tables
        db.create_all()
        
        # Check if we have any clusters
        if Cluster.query.count() == 0:
            # Add default clusters
            default_clusters = [
                Cluster(name='prod-us-east', display_name='Production US East', is_active=True),
                Cluster(name='prod-eu-west', display_name='Production EU West', is_active=True),
                Cluster(name='staging', display_name='Staging Environment', is_active=True),
            ]
            
            for cluster in default_clusters:
                db.session.add(cluster)
            
            db.session.commit()
            print("Default clusters created successfully!")
        
        print("Database initialization completed!")

if __name__ == '__main__':
    init_database()
