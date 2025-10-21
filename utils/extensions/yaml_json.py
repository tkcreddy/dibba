import yaml
import json

yaml_content = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx-deployment
spec:
  selector:
    matchLabels:
      app: nginx
  replicas: 2 # tells deployment to run 2 pods matching the template
  template:
    metadata:
      labels:
        app: nginx
    spec:
      containers:
      - name: nginx
        image: nginx:1.14.2
        ports:
        - containerPort: 80
"""

# Convert YAML to Python dictionary
yaml_dict = yaml.safe_load(yaml_content)

# Convert Python dictionary to JSON
json_data = json.dumps(yaml_dict, indent=2)

print(json_data)
