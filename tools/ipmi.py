import subprocess

def check_temps(host: str = "local"):
    """
    Reads temperature sensors via IPMI.
    Args:
        host: 'local' to check this server, or an IP for remote IPMI.
    """
    # Note: Requires 'ipmitool' installed on the system
    # sudo apt install ipmitool
    cmd = ["sudo", "ipmitool", "sdr", "type", "temperature"]
    
    if host != "local":
        # Add remote flags if needed, e.g. -H <host> -U <user> -P <pass>
        # For now, let's assume we just run it locally or via the SSH tool for remotes
        return "Remote IPMI not configured in python yet. Use run_remote_cmd instead."

    try:
        result = subprocess.check_output(cmd).decode()
        return result
    except Exception as e:
        return f"IPMI Error (is ipmitool installed?): {e}"
