#cloud-config
# terraform/cloud-init.yaml.tpl
# Rendered by Terraform templatefile() — all $${var} references are injected at apply time.

package_update: true
package_upgrade: true

packages:
  - git
  - nginx
  - software-properties-common
  - curl

runcmd:
  # Disable ufw to prevent accidental port blocking
  - ufw disable || true

  # Install Python 3.12 via deadsnakes PPA
  - add-apt-repository ppa:deadsnakes/ppa -y
  - apt-get update -y
  - apt-get install -y python3.12 python3.12-venv python3.12-distutils

  # Create deploy user
  - useradd -m -s /bin/bash deploy
  - mkdir -p /home/deploy/.ssh
  - chmod 700 /home/deploy/.ssh

  # Install SSH public key for deploy user
  - echo "${ssh_public_key}" > /home/deploy/.ssh/authorized_keys
  - chmod 600 /home/deploy/.ssh/authorized_keys

  # Install GitHub deploy key (read-only key for repo clone)
  - |
    cat > /home/deploy/.ssh/id_ed25519 << 'SSHKEY'
    ${gh_deploy_key}
    SSHKEY
  - chmod 600 /home/deploy/.ssh/id_ed25519

  # Configure SSH to use deploy key for github.com
  - |
    cat > /home/deploy/.ssh/config << 'SSHCONFIG'
    Host github.com
      HostName github.com
      User git
      IdentityFile /home/deploy/.ssh/id_ed25519
      StrictHostKeyChecking no
    SSHCONFIG
  - chmod 600 /home/deploy/.ssh/config
  - chown -R deploy:deploy /home/deploy/.ssh

  # Clone repo as deploy user
  - sudo -u deploy git clone git@github.com:${github_repo}.git /home/deploy/shopping-agent

  # Create virtualenv
  - sudo -u deploy python3.12 -m venv /home/deploy/venv

  # Install app dependencies
  - sudo -u deploy /home/deploy/venv/bin/pip install --upgrade pip
  - sudo -u deploy /home/deploy/venv/bin/pip install -e /home/deploy/shopping-agent

  # Write Cloudflare Origin Certificate
  - |
    cat > /etc/ssl/cloudflare-origin.pem << 'CERT'
    ${cf_origin_cert}
    CERT
  - |
    cat > /etc/ssl/cloudflare-origin-key.pem << 'KEY'
    ${cf_origin_key}
    KEY
  - chmod 600 /etc/ssl/cloudflare-origin-key.pem

  # Write nginx config
  - |
    cat > /etc/nginx/sites-available/shopping-agent << 'NGINX'
    server {
        listen 80;
        server_name shopping.aydho.com;
        return 301 https://$host$request_uri;
    }

    server {
        listen 443 ssl;
        server_name shopping.aydho.com;

        ssl_certificate     /etc/ssl/cloudflare-origin.pem;
        ssl_certificate_key /etc/ssl/cloudflare-origin-key.pem;

        location / {
            proxy_pass         http://127.0.0.1:8000;
            proxy_set_header   Host $host;
            proxy_set_header   X-Real-IP $remote_addr;
            proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header   X-Forwarded-Proto $scheme;
        }
    }
    NGINX
  - ln -sf /etc/nginx/sites-available/shopping-agent /etc/nginx/sites-enabled/shopping-agent
  - rm -f /etc/nginx/sites-enabled/default
  - nginx -t

  # Write systemd service unit
  - |
    cat > /etc/systemd/system/shopping-agent.service << 'SYSTEMD'
    [Unit]
    Description=Shopping Agent
    After=network.target

    [Service]
    User=deploy
    WorkingDirectory=/home/deploy/shopping-agent
    EnvironmentFile=/home/deploy/shopping-agent/.env
    ExecStart=/home/deploy/venv/bin/uvicorn shopping_agent.main:app --host 127.0.0.1 --port 8000
    Restart=always
    RestartSec=5

    [Install]
    WantedBy=multi-user.target
    SYSTEMD

  # Write .env file (systemd EnvironmentFile is canonical source; app's load_dotenv() is a no-op)
  - |
    cat > /home/deploy/shopping-agent/.env << 'ENVFILE'
    DATABASE_URL=${database_url}
    COLES_API_KEY=${coles_api_key}
    WOOLWORTHS_API_KEY=${woolworths_api_key}
    DEBUG=false
    ENVFILE
  - chmod 600 /home/deploy/shopping-agent/.env
  - chown deploy:deploy /home/deploy/shopping-agent/.env

  # Sudoers rule — deploy user can restart service without password
  - echo "deploy ALL=(ALL) NOPASSWD: /bin/systemctl restart shopping-agent" > /etc/sudoers.d/deploy-restart
  - chmod 440 /etc/sudoers.d/deploy-restart

  # Fix ownership on all deploy user files
  - chown -R deploy:deploy /home/deploy/shopping-agent
  - chown -R deploy:deploy /home/deploy/venv

  # Enable and start services
  - systemctl daemon-reload
  - systemctl enable shopping-agent
  - systemctl start shopping-agent
  - systemctl enable nginx
  - systemctl restart nginx

  # Write sentinel file — signals bootstrap is complete
  - touch /home/deploy/.cloud-init-done
