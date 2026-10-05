const core = require('@actions/core');
const io = require('@actions/io');
const exec = require('@actions/exec');
const {DefaultArtifactClient} = require('@actions/artifact');
const glob = require('@actions/glob');

const fs = require('fs');
const path = require('path');
const os = require('os');

// Root of the build checkout. prepare-environment picks the drive with the most free space
// and exports it as HELIUM_ROOT; C: is the historical default.
const ROOT_DIR = process.env.HELIUM_ROOT || 'C:\\helium-windows';
const BUILD_DIR = path.join(ROOT_DIR, 'build');
// The multi-GB archives are parked on the *other* drive, so they don't share a disk with the
// build tree.
const SCRATCH_DRIVE = ROOT_DIR.toUpperCase().startsWith('D:') ? 'C:' : 'D:';
const SCRATCH_CANDIDATE = `${SCRATCH_DRIVE}\\helium-scratch`;
const FALLBACK_SCRATCH = BUILD_DIR;
// Headroom kept free on D: besides the archive itself.
const SCRATCH_MARGIN_BYTES = 1024 ** 3;

async function freeBytes(dir) {
    const st = await fs.promises.statfs(dir);
    return Number(st.bavail) * Number(st.bsize);
}

// The multi-GB artifacts.zip is parked on the drive that does not hold the build tree (when
// present and big enough), so the two don't compete for the same disk. Falls back to the
// build directory otherwise.
async function pickScratchDir(neededBytes, label) {
    const drive = `${SCRATCH_DRIVE}\\`;
    try {
        await fs.promises.access(drive);
        const free = await freeBytes(drive);
        const gb = b => (b / 1024 ** 3).toFixed(2);
        console.log(`[${label}] ${SCRATCH_DRIVE} free ${gb(free)} GB, need ${gb(neededBytes + SCRATCH_MARGIN_BYTES)} GB`);
        if (free >= neededBytes + SCRATCH_MARGIN_BYTES) {
            await io.mkdirP(SCRATCH_CANDIDATE);
            return SCRATCH_CANDIDATE;
        }
        console.log(`[${label}] ${SCRATCH_DRIVE} too small, falling back to ${BUILD_DIR}`);
    } catch (e) {
        console.log(`[${label}] ${SCRATCH_DRIVE} unavailable (${e.message}), falling back to ${BUILD_DIR}`);
    }
    return FALLBACK_SCRATCH;
}

async function logDiskSpace(label) {
    try {
        const { stdout } = await exec.getExecOutput('powershell', [
            '-NoProfile',
            '-Command', 
            'Get-PSDrive C | Select-Object @{N="Used(GB)";E={[math]::Round($_.Used/1GB,2)}}, @{N="Free(GB)";E={[math]::Round($_.Free/1GB,2)}} | Format-Table -AutoSize'
        ], { silent: true });
        console.log(`\n=== Disk space: ${label} ===`);
        console.log(stdout);
    } catch (e) {
        console.log(`Warning: Could not log disk space: ${e.message}`);
    }
}

async function run() {
    const started_at = Number(process.env.HELIUM_JOB_STARTED_AT) || Math.floor(Date.now() / 1000);

    process.on('SIGINT', function() {
    })
    const from_artifact = core.getBooleanInput('from_artifact', {required: true});
    const upload_final = core.getBooleanInput('upload_final', {required: false});
    const ntfs_compression = core.getBooleanInput('ntfs_compression', {required: false});

    const arm = core.getBooleanInput('arm', {required: false})
    console.log(`artifact: ${from_artifact}, upload_final: ${upload_final}`);

    const artifact = new DefaultArtifactClient();
    const artifactName = arm ? 'build-artifact-arm64' : 'build-artifact-x86_64';
    
    if (from_artifact && !upload_final) {
        await logDiskSpace('Before artifact download');
        
        const artifactInfo = await artifact.getArtifact(artifactName);
        let scratchDir = await pickScratchDir(Number(artifactInfo.artifact.size) || 0, 'download');
        try {
            await artifact.downloadArtifact(artifactInfo.artifact.id, {path: scratchDir});
        } catch (e) {
            if (scratchDir === FALLBACK_SCRATCH) throw e;
            console.log(`Download to ${scratchDir} failed (${e.message}), retrying on C:...`);
            await io.rmRF(scratchDir);
            scratchDir = FALLBACK_SCRATCH;
            await artifact.downloadArtifact(artifactInfo.artifact.id, {path: scratchDir});
        }

        await logDiskSpace(`After artifact download (ZIP still present, in ${scratchDir})`);
        
        const zipPath = path.join(scratchDir, 'artifacts.zip');
        
        // Extract ZIP
        console.log('Extracting artifacts.zip...');
        await exec.exec('7z', ['x', zipPath, `-o${BUILD_DIR}`, '-y']);
        
        await logDiskSpace('After ZIP extraction (ZIP still present)');
        
        // Delete ZIP immediately after extraction - CRITICAL for disk space
        console.log('Deleting artifacts.zip immediately after extraction...');
        await io.rmRF(zipPath);
        
        await logDiskSpace('After deleting artifacts.zip');
        
        if (ntfs_compression) {
            // The extracted tree lands uncompressed; shrink it in place before the build resumes.
            console.log('Applying NTFS compression to build directory...');
            await exec.exec('compact', ['/c', `/s:${BUILD_DIR}`, '/i'], {ignoreReturnCode: true});

            await logDiskSpace('After NTFS compression of build directory');
        } else {
            console.log('NTFS compression disabled; leaving build directory uncompressed.');
        }
        
    } else if (!upload_final) {
        await io.mkdirP(BUILD_DIR);
        if (ntfs_compression) {
            // Mark the (still empty) build dir compressed so everything build.py writes into it
            // inherits NTFS compression automatically, with no retroactive scan needed.
            await exec.exec('compact', ['/c', BUILD_DIR], {ignoreReturnCode: true});

            await logDiskSpace('After marking build directory for compression');
        } else {
            console.log('NTFS compression disabled; build directory stays uncompressed.');
        }
    }

    const args = ['build.py', '--ci', String(started_at)]
    // CI only ships the portable ZIP, so skip the setup/mini_installer targets.
    args.push('--portable-only')
    if (process.env.HELIUM_BUILD_JOBS) {
        args.push('-j', process.env.HELIUM_BUILD_JOBS);
    } else if (process.env.RUNNER_ENVIRONMENT === 'github-hosted') {
        const jobs = Math.max(1, Math.min(os.availableParallelism(),
            Math.floor(os.freemem() / (3 * 1024 ** 3))));
        args.push('-j', String(jobs));
    }

    if (arm)
        args.push('--arm')

    if (upload_final) {
        const finalDirectory = core.getInput('final_directory', {required: true});
        const globber = await glob.create(path.join(finalDirectory, 'helium*'),
            {matchDirectories: false});
        let packageList = await globber.glob();
        const finalArtifactName = arm ? 'helium-arm64' : 'helium-x86_64';
        const maxUploadAttempts = 5;
        
        await logDiskSpace('Before final artifact upload');
        
        for (let attempt = 1; attempt <= maxUploadAttempts; ++attempt) {
            try {
                await artifact.deleteArtifact(finalArtifactName);
            } catch (e) {
                // ignored
            }
            try {
                await artifact.uploadArtifact(finalArtifactName, packageList,
                    finalDirectory, { retentionDays: 4, compressionLevel: 0 });
                break;
            } catch (e) {
                console.error(`Upload artifact failed: ${e}`);
                if (attempt === maxUploadAttempts) throw e;
                // Wait 10 seconds between the attempts
                await new Promise(r => setTimeout(r, 10000));
            }
        }

        // The GitHub workspace is emptied after the repo is copied to the build root
        // (see prepare-environment), so the checkout has to be read from the copy.
        const { exitCode, stdout } = await exec.getExecOutput('python', [
            'helium-chromium\\utils\\helium_version.py',
            '--print',
            '--tree', 'helium-chromium',
            '--platform-tree', '.'
        ], { cwd: ROOT_DIR });

        if (exitCode !== 0) throw `failed getting version: ${exitCode}`;
        core.setOutput('version', stdout.trim());
        core.setOutput('finished', true);
        return;
    }

    await exec.exec('python', ['-m', 'pip', 'install', 'httplib2==0.22.0', 'Pillow', 'clang-format'], {
        cwd: ROOT_DIR,
        ignoreReturnCode: true
    });
    
    await logDiskSpace('Before build.py');
    
    const retCode = await exec.exec('python', args, {
        cwd: ROOT_DIR,
        ignoreReturnCode: true
    });

    await logDiskSpace('After build.py completed');

    if (retCode > 0 && retCode !== 42) {
        throw `Unexpected return code: ${retCode}`
    }

    core.setOutput('finished', retCode === 0);

    const package_here = retCode === 0 && core.getBooleanInput('package_here') &&
        Date.now() / 1000 - started_at < 5 * 60 * 60;
    core.setOutput('package_here', package_here);

    if (!package_here && core.getBooleanInput('save_artifact')) {
        await logDiskSpace('Before creating artifacts.zip');
        
        console.log('Creating artifacts.zip...');
        // Archive size isn't known up front; try the other drive when it has a reasonable
        // amount of room, and redo it in the build directory if 7z fails there (e.g. the disk
        // filled up). Exit code 1 only
        // means some files couldn't be read (the archive is still complete), so it must not
        // trigger the retry: redoing a 28 GiB zip costs ~15 minutes.
        let zipDir = await pickScratchDir(8 * 1024 ** 3, 'zip');
        let zipPath = path.join(zipDir, 'artifacts.zip');
        const zipArgs = out => ['a', '-tzip', out, path.join(BUILD_DIR, 'src'), '-mx=3', '-mtc=on'];
        let zipCode = await exec.exec('7z', zipArgs(zipPath), {ignoreReturnCode: true});
        if (zipCode > 1 && zipDir !== FALLBACK_SCRATCH) {
            console.log(`7z failed on ${zipDir} (exit ${zipCode}), retrying in ${FALLBACK_SCRATCH}...`);
            await io.rmRF(zipDir);
            zipDir = FALLBACK_SCRATCH;
            zipPath = path.join(zipDir, 'artifacts.zip');
            await exec.exec('7z', zipArgs(zipPath), {ignoreReturnCode: true});
        }
        
        await logDiskSpace('After creating artifacts.zip');
        
        for (let i = 0; i < 5; ++i) {
            try {
                await artifact.deleteArtifact(artifactName);
            } catch (e) {
                // ignored
            }
            try {
                console.log(`Uploading artifact (attempt ${i + 1}/5)...`);
                await artifact.uploadArtifact(artifactName, [zipPath],
                    zipDir, { retentionDays: 4, compressionLevel: 0 });
                
                // After successful upload, delete the ZIP
                console.log('Deleting artifacts.zip after successful upload...');
                await io.rmRF(zipPath);
                
                await logDiskSpace('After uploading and deleting artifacts.zip');
                break;
            } catch (e) {
                console.error(`Upload artifact failed: ${e}`);
                // Wait 10 seconds between the attempts
                await new Promise(r => setTimeout(r, 10000));
            }
        }
    }
}

run().catch(err => core.setFailed(err.message));
