# Regulations used by the plan check

Each rule the checker applies is listed here with the exact clause it comes from, a link to the official text and a local copy in `sources/`.
Quotes are copied from the official texts. English translations of Finnish clauses are ours and are marked as such.

| Rule | Where | Status | Clause |
|---|---|---|---|
| `FI-848-2017-38` | Finland | statutory | 38 § Palovaroitin ja paloilmoitin, Taulukko 12 |
| `UK-BB103-CLA12` | England | guidance (non-statutory) | Part A, Basic teaching area: classrooms, and Area Data Sheet CLA12 |
| `FI-1009-2017-9` | Finland | statutory | 9 § Ulkoilmavirrat |

## FI-848-2017-38

**Decree of the Ministry of the Environment on the Fire Safety of Buildings (848/2017)**

- Clause: 38 § Palovaroitin ja paloilmoitin, Taulukko 12
- Status: statutory
- Official text: https://www.finlex.fi/fi/lainsaadanto/saadoskokoelma/2017/848
- Local copy: `sources/fi_848_2017.xml`

> Asuntojen, majoitustilojen, hoitolaitosten, päivähoitolaitosten, päiväkotien ja muiden varhaiskasvatuksen tilojen sekä koulujen on oltava varustettu tarkoituksenmukaisella laitteistolla, joka varhaisessa vaiheessa ilmoittaa alkavasta palosta.

Dwellings, accommodation premises, care institutions, day-care institutions, day-care centres and other early childhood education premises, and schools must be equipped with an appropriate device that gives warning of an incipient fire at an early stage. (our translation)

Taulukko 12. Tiloissa edellytetyt palosta ilmoittavat laitteistot, row 'Koulut':

- Enintään 250 oppilasta: sähköverkkoon kytketty palovaroitin (up to 250 pupils: mains-connected smoke alarm)
- 251–500 oppilasta: paloilmoitin (251 to 500 pupils: fire alarm system)
- Yli 500 oppilasta: hätäkeskukseen kytketty paloilmoitin (over 500 pupils: fire alarm system connected to the emergency response centre)

What the check does with it: The decree requires the school to HAVE an appropriate early-warning device and says which kind. It does not list the rooms. The room-by-room check (a detector in every room) applies the decree's word 'tarkoituksenmukainen' (appropriate) as full coverage, which is how detection systems are laid out in practice; the placement standard itself is not quoted here.

## UK-BB103-CLA12

**Building Bulletin 103: Area guidelines for mainstream schools (Department for Education)**

- Clause: Part A, Basic teaching area: classrooms, and Area Data Sheet CLA12
- Status: guidance (non-statutory)
- Official text: https://www.gov.uk/government/publications/mainstream-schools-area-guidelines
- Local copy: `sources/uk_bb103.pdf`, page 18

> CLA12 C General classroom: For a range of FF&E layout options and activities 55m2 for 30

> typically, standard classrooms in zone C (55m2 to 62m2 for 30 pupils) [...] semi-open classbases in zone B (50m2 to 55m2 for 30 pupils) which are restrictive

What the check does with it: Guidance, not law: the document calls itself 'simple, non-statutory area guidelines'. It is written for mainstream schools; the sample plan is a special school, for which Building Bulletin 104 recommends larger rooms for smaller groups, so this check is the lenient one.

## FI-1009-2017-9

**Decree of the Ministry of the Environment on the Indoor Climate and Ventilation of New Buildings (1009/2017)**

- Clause: 9 § Ulkoilmavirrat
- Status: statutory
- Official text: https://www.finlex.fi/fi/lainsaadanto/saadoskokoelma/2017/1009
- Local copy: `sources/fi_1009_2017.xml`

> Oleskelutilojen ulkoilmavirraksi on mitoitettava vähintään 6 dm³/s henkilöä kohti suunniteltuna käyttöaikana, jos tilan käyttötarkoituksesta ei aiheudu lisäilmavirran tarvetta.

The outdoor air flow of occupied spaces must be designed to be at least 6 dm³/s per person during the designed period of use, unless the use of the space requires additional air flow. (our translation)

What the check does with it: The required air flow follows from the number of people the room is furnished for. Whether the ventilation delivers it cannot be read from an architectural plan; the check reports the required flow and marks the rule as not checked.
