# Automated TRACE to NAS Backup

A Python script for automatically backing up files from the TRACE cluster to Network Attached Storage (NAS) using SSH and `rsync`.

### Features
- Copies only new or modified files.
- Preserves older versions of modified files.
- Never deletes files from the NAS.
- Supports directory exclusions.
- Generates daily CSV backup logs.
- Supports manual and scheduled backups.

## 1. Configure Passwordless SSH Access to TRACE

**Generate an SSH key:**

```bash
ssh-keygen -t ed25519
```

**Copy the public key:**

```bash
cat ~/.ssh/id_ed25519.pub
```

Submit the public key through the TRACE SSH key registration portal and wait for administrator approval.

**Configure SSH aliases:**

Edit `~/.ssh/config`:

```sshconfig
Host trace
    HostName YOUR_TRACE_LOGIN_HOST
    User YOUR_TRACE_USERNAME

Host data
    HostName YOUR_TRACE_DATA_TRANSFER_HOST
    User YOUR_TRACE_USERNAME
```

Verify login:

```bash
ssh trace
```

The `data` alias is used for file transfers.

## 2. Mount the NAS

Contact the NAS administrator to obtain an account, access permissions, and the NAS connection details.

**Create a local mount directory:**

```bash
mkdir -p ~/nas-backup
```

**Mount the NAS:**

```bash
sudo mount -t cifs \
  -o username=YOUR_NAS_USERNAME,uid=$(id -u),gid=$(id -g),vers=3.0 \
  //YOUR_NAS_SERVER/YOUR_NAS_SHARE \
  ~/nas-backup
```

Enter the NAS password when prompted.

**Verify the mount:**

```bash
mountpoint ~/nas-backup
```

The NAS must be mounted before running a backup.

## 3. Configure the Backup

Clone the repository:

```bash
git clone https://github.com/shahid-ahmed-96/trace-nas-backup.git
cd trace-nas-backup
```

Edit the configuration file:

```bash
vi backup_config.ini
```

Replace the placeholders with your own values:

- `ssh_host`: SSH alias for the data transfer node (`data`).
- `source`: Directory on TRACE to back up.
- `mount_point`: Local NAS mount directory.
- `destination`: Backup destination on the NAS.
- `log_dir`: Directory on TRACE for backup logs.
- `exclude_dirs`: Directories to exclude.

**Only `backup_config.ini` needs to be edited for the backup script.** Ensure the configured mount point matches the directory created in Step 2.

## 4. Run the Backup

**Preview files without copying:**

```bash
python3 backup.py now --dry-run
```

**Run the backup immediately:**

```bash
python3 backup.py now
```

The script checks the NAS mount and TRACE connection before transferring files.

## 5. Schedule Weekly Backups

The script supports scheduled execution:

```bash
python3 backup.py scheduled
```

A systemd timer can be configured to execute this command automatically every **Friday at 5:00 PM**.

The local computer must be running, connected to the network, and have the NAS mounted.


##5. Schedule Automatic Weekly Backups Using Cron

Open the crontab editor:

```text
crontab -e
```

Add the following line to schedule a backup every Friday at 5:00 PM:

```text
0 17 * * 5 cd /home/YOUR_LOCAL_USERNAME/trace-nas-backup && /usr/bin/python3 backup.py scheduled
```

Replace YOUR_LOCAL_USERNAME with your local machine username and adjust the directory if necessary.

Save and exit the editor.

Verify the scheduled job:

```text
crontab -l
```

The backup will run automatically at the scheduled time, provided the computer is running, the network is accessible, and the NAS is mounted.

## 6. Backup Logs

Weekly CSV logs are saved in the TRACE directory specified by `log_dir`:

```text
backuplog.YYYY_MM_DD.csv
```

Local copies are also stored in:

```text
~/.trace_backup/logs/
```

Logs record new files, modified files, and failed transfers.

---

**Note:** The current implementation is designed for Linux systems. 

