# Form and arrangement

Does the piece have a shape over time, and does the arrangement build it?

This is where machine-composed music most reliably falls down, and it is weighted accordingly.
A composer working bar by bar has every local incentive to keep the texture that is working and no incentive to take anything away.
The result is eight good bars looped sixteen times: correct, and formless.

Judge two things together.

**Form** is the sequence of sections and their relationships: how many there are, whether their material is genuinely distinct, whether the order creates an arc, and whether the transitions between them are composed or merely adjacent.
**Arrangement** is what plays in each section: which instruments enter, which drop out, how density and register change, and whether those changes are what makes each section feel different from the last.

The test for a section is whether removing it would be noticed.
The test for an arc is whether you can name the climax and say why it lands where it does.
The test for a transition is whether the last bar of a section prepares the first bar of the next: a fill, a held chord, a drop, a lift, a break.
Sections that simply stop and start are a symptom of a piece assembled rather than composed.

Length matters here too.
A piece that reaches its full texture in bar 1 and stays there has spent everything immediately, and nothing later can register as an increase.

## Anchored scale

**2 = one section of material repeated with no variation for the whole piece.**
The same bars loop from start to finish, every instrument that plays plays throughout, and there is no point at which the piece is doing something different from what it did at the start.
Also 2 for a piece whose declared sections all contain the same material.

**4 = two distinguishable sections, but contrast is only dynamic or density.**
You can hear a change, and the change is that something got louder or an extra part came in.
Both sections use the same harmonic material, the same melodic figure, and the same groove.
Transitions are cuts.
Nothing exits: instruments accumulate and never leave.

**6 = clear multi-section form with genuinely distinct material.**
Something like ABAB or ABAC, where B is different from A in more than one dimension (its own melodic figure, its own harmony, its own texture), and the return of A is recognisable as a return.
The arrangement differentiates sections by orchestration as well as density, and at least one instrument drops out somewhere.
What is missing is an arc: the sections are distinct but interchangeable in order, transitions are functional rather than composed, and no single point is the peak.

**8 = a multi-section arc with a bridge or reduced section, and a climax placed deliberately.**
The form goes somewhere: there is a section that pulls back (a breakdown, a bridge, a stripped verse) which makes the section after it land harder, and there is an identifiable high point positioned so that roughly the last third of the piece carries the most weight.
Entrances and exits are staged rather than simultaneous, transitions are written (a fill into the drop, a held chord over the bar line, a two-bar break), and the ending is an ending rather than a stop.

**10 = every section earns its place, transitions are engineered, the arc is inevitable in retrospect.**
You could not reorder, remove, or shorten a section without damage.
Density, register, and orchestration form a single curve across the whole piece, with the climax as its apex and the reduction before it as its setup.
Every transition does specific work, the final section resolves material introduced at the start, and the length is exactly what the material supports.

## Between the anchors

A 5 is a distinct-sections form where every part still plays in every section.
A 7 has a real arc but a climax that arrives too early to hold.
A 9 is a 10 with one transition that is merely a cut.

## Reading the evidence

The SECTIONS block gives declared section names, start bars, and lengths.
If it says "none declared", form must be inferred from the notes, and you should say so in the rationale; a composer who declares no sections has usually not thought in sections.
The DENSITY table is the single most useful piece of evidence for this dimension: it reports notes per bar per track per section, so instrument entrances (a zero becoming a number), exits (a number becoming zero), and density curves are visible at a glance.
A density table whose columns are near-constant across all rows describes a piece with no arrangement, whatever its section labels say.
The REPETITION percentage bounds the score: above roughly 70 percent of sounding bars being exact repeats, this dimension cannot exceed 4.
On the piano-roll image, form is the most legible thing in the picture: look for horizontal bands that start and stop, and for the vertical extent of the texture changing over time.
A picture that looks the same left to right is a piece with no form.

## What a finding needs

Every finding must carry:

* a bar range (`bar_start` and `bar_end`) marking where the formal problem is, and a `track` name when the problem is one part's presence or absence
* the specific structural fault: which section, which transition, which instrument that should have entered or left
* a `suggested_revision` naming the concrete arrangement change

"Needs more variation" is not a finding.
"Bars 32-47 are labelled chorus but the density table shows identical notes-per-bar for every track as the preceding verse, so the chorus does not arrive; drop the pads and hats for bars 30-31 and bring all four parts back in on the downbeat of bar 32" is a finding.
