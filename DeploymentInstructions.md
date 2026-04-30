# Clone or create the project directory
mkdir k8s-contact-manager
cd k8s-contact-manager

# Create all the files as shown above

# Build and start the services
docker-compose up -d

# Initialize the database
docker-compose exec app python init_db.py

# Access the application
# Open http://localhost:5000
