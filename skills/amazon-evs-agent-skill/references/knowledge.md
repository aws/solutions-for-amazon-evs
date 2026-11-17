# Sources for EVS questions

Answer the user's Amazon EVS question from the official AWS documentation
listed below, following the rules in [SKILL.md](../SKILL.md).

## Scope

Q&A answers questions. It doesn't run AWS commands, call AWS APIs, or read or
change anything in the user's account.

- **Preparing, deploying, monitoring, fixing, or tearing down an EDO
  environment:** switch to the guided deployment workflow in `SKILL.md` and
  keep the user's inputs. For deployment without EDO, point to the User
  Guide's [Getting started](https://docs.aws.amazon.com/evs/latest/userguide/getting-started.html).
  Sizing inputs are in scope; calculating a real workload's host count isn't.
- **Running a command or looking at the user's resources** outside a
  deployment task: say Q&A doesn't do that, and give the API or CLI reference
  page so the user can run it.

## Sources

Start from these pages:

| Page | URL | Use for |
|---|---|---|
| Documentation landing page | https://docs.aws.amazon.com/evs/ | Links to every source below |
| User Guide | https://docs.aws.amazon.com/evs/latest/userguide/what-is-evs.html | Concepts, architecture, setup and the deployment checklist, getting started, migration (HCX), managing environments (VCF subscriptions, VCF versions and EC2 instance types, lifecycle management, maintenance, secrets, hosts, connectors), Windows Server entitlements, the Custom Addon depot, security and IAM, resilience, other services (CloudFormation, FSx for NetApp ONTAP), troubleshooting, CloudTrail, service quotas, document history |
| API Reference | https://docs.aws.amazon.com/evs/latest/APIReference/Welcome.html | Actions, data types, service-specific errors, common parameters, common error types |
| AWS CLI v2 `evs` reference | https://awscli.amazonaws.com/v2/documentation/api/latest/reference/evs/index.html | Exact `aws evs` command names and options (to cite, not to run) |
| CloudFormation `AWS::EVS` reference | https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/AWS_EVS.html | `AWS::EVS` resource types and their properties |
| AWS General Reference: Amazon EVS endpoints and quotas | https://docs.aws.amazon.com/general/latest/gr/evs.html | The Regions where Amazon EVS is available, service endpoints, and default service quota values. The User Guide's **Service quotas** page links here for the numbers |

Stay within these pages and the pages their indexes link to. When an EVS page
defers to another page on `docs.aws.amazon.com` (for example, EC2 Capacity
Reservations, Direct Connect, IPAM, or Secrets Manager), that page counts as
documentation: follow the link, and any further links on that page that stay
on the question's topic, and cite it. Do this before
saying you don't know.

Some EVS pages link outside the AWS documentation, for example to Broadcom's
VCF documentation or to an AWS pricing page. What the EVS page itself says is
a source you can cite. Don't answer from the linked page. If it's where the
rest of the answer lives, give the link and say it's outside the Amazon EVS
documentation.

One exception: for what Amazon EVS bills, what each billed item is called
(including what the AWS Pricing Calculator calls it), and its listed price,
read and cite the [Amazon EVS pricing page](https://aws.amazon.com/evs/pricing/).
Follow SKILL.md's price rule: quote prices as the page states them, tell the
user to confirm them there and in the AWS Pricing Calculator, and compute no
totals.

## Find and read pages

- Each guide lists every page with a one-line summary in `llms.txt`:
  - User Guide: https://docs.aws.amazon.com/evs/latest/userguide/llms.txt
  - API Reference: https://docs.aws.amazon.com/evs/latest/APIReference/llms.txt
- Read a page's `.md` version for plain text; cite its `.html` URL. To search
  the whole User Guide for a term, use
  https://docs.aws.amazon.com/evs/latest/userguide/full.md,
  then read and cite the individual page.
- Follow "For more information, see…" links until you reach the page that
  states the fact.

## Answering

- Cite the pages you used, with their `.html` URLs.
- When a fact differs by deployment mode (EVS-deployed or self-deployed), say
  which mode it applies to; EDO uses self-deployed mode.
- For what EDO supports or does, read the matching release README (core.md,
  "Use the matching EDO release"); it wins over the User Guide. Also read the
  User Guide's page on it, and if they differ, cite both and say which applies.
- For supported versions, Regions, instance types, and limits, also check the
  User Guide's **Document history**.
- Match the user's level: if they aren't technical, explain each technical
  term the first time you use it.
- If the documentation doesn't cover something, say so plainly, name the pages
  you checked, and don't fill the gap from memory.
