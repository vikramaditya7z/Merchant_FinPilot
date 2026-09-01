import { runFrontendIncidentJobsTests } from './incident_jobs.test';

declare const process: { exit: (code: number) => void };

async function main() {
  console.log('Running Frontend Incident Jobs Console Test Suite...\n');
  const result = await runFrontendIncidentJobsTests();

  for (const t of result.tests) {
    console.log(`  ${t}`);
  }

  console.log(`\nResults: ${result.passed} passed, ${result.failed} failed.`);

  if (result.failed > 0) {
    process.exit(1);
  }
}

main().catch((err) => {
  console.error('Test runner fatal error:', err);
  process.exit(1);
});
