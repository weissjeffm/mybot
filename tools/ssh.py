import paramiko

# Keep client global to reuse connections
ssh_client = paramiko.SSHClient()
ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

def run_remote_cmd(command: str, hostname: str, user: str = "gamer"):
    """
    Executes a shell command on a remote machine via SSH.
    Args:
        command: The bash command to run.
        hostname: IP or hostname (e.g., '192.168.1.50').
        user: Username to login as (default: 'gamer').
    """
    try:
        # Check if we need to reconnect or switch hosts
        # (Simplified logic: if hostname differs, close and reconnect)
        # For production, you'd manage a dictionary of clients {host: client}
        ssh_client.connect(hostname, username=user)
        
        stdin, stdout, stderr = ssh_client.exec_command(command, timeout=10)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        
        if err:
            return f"STDERR: {err}\nSTDOUT: {out}"
        return out if out else "Success (No Output)"
    except Exception as e:
        return f"SSH Error: {e}"
