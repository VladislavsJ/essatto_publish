"""Multilingual vocabulary: words -> canonical kinds / room types / aggregates. Extend here, not in solver code.
Order matters inside each list: the first matching entry wins, so put specific phrases before generic ones."""
import re

LANGS = "en de fr it fi sv et nl es pl da ro"

# --- what is being asked about (entity kinds). (kind, regex) ----------------------------------------------
KINDS = [
 ("floor",
  r"how many floors|floors\s*\(storeys\)|number of (?:floors|storeys)|count (?:of |the |all )?(?:floors|storeys)"
  r"|\bfloors\s+(?:does|do|are|shown|depicted|displayed|drawn|in this|on this)|total (?:number of )?floors"
  r"|\bstoreys?\b|\bstories\b|\bkerrost[aä]\b|\bkerroksia\b|\bkerrosten\b|kuinka monta kerrosta"
  r"|\bvåningar\w*\b|hur många (?:plan|våningsplan)\b|antal(?:et)? våningsplan\b"
  r"|\bmitu korrust\b|\bkorrust\b|\bkorruste\b|\bkorruseid\b"
  r"|\bstockwerke\w*\b|\bgeschosse\w*\b|\betagen\b|wie viele geschosse|anzahl der (?:geschosse|stockwerke|etagen)"
  r"|combien d'étages|nombre (?:total |global |exact )?d'étages|\bétages\s+(?:sont|figurent|montre|comporte|compte|représent|affich|visibl)"
  r"|quanti piani|numero [a-zà-ú ]*di piani|\bpiani\s+(?:sono|mostra|presenti|raffigurat|rappresentat|visibil)"
  r"|hoeveel (?:verdiepingen|bouwlagen)|aantal (?:verdiepingen|bouwlagen)|\b(?:verdiepingen|bouwlagen)\s+(?:telt|toont|zijn|staan|bevat)"
  r"|cuántas plantas|cuántos pisos|número [a-zà-ú ]*de (?:plantas|pisos|niveles)|\b(?:plantas|pisos)\s+(?:muestra|tiene|hay|aparecen|se muestran|figuran)"
  r"|ile (?:kondygnacji|pięter)|liczb[aęy]\s+(?:kondygnacji|pięter)|(?:kondygnacji|pięter)\s+(?:pokazuje|przedstawia|obejmuje|ukazuje|ma|jest|widać)"),

 ("apartment",
  r"\bapartments?\b|\bapts?\b|residential units?|dwelling units?|housing units?|\bdwellings?\b"
  r"|\bwohnungen\b|\bwohnung\b|\bwohneinheit\w*\b|\bwhg\.?\b"
  r"|\blogements?\b|\bappartements?\b|unités?\s+d'habitation|unités?\s+résidentielles?"
  r"|\bappartament[oi]\b|\balloggi\b|unità\s+abitativ\w*"
  r"|\basunto\w*\b|\basunno\w*\b|\b(?:asuin)?huoneisto\w*\b"
  r"|\blägenhet\w*\b|\bbostadsenhet\w*\b|\bbostäder\w*\b"
  r"|\bkorter\w*\b|\beluruum\w*\b"
  r"|\bwooneenhe\w*\b|\bappartementen\b|\bwoningen\b"
  r"|\bapartamentos?\b|\bviviendas?\b|unidades?\s+residenciales?"
  r"|\bmieszka(?:ń|nia|nie|niu|niach|niom)\b|jednost\w*\s+mieszkaln\w*|lokal\w*\s+mieszkaln\w*"
  r"|\blejlighed\w*\b"),

 ("toilet",
  r"toilet\s+bowls?|wc\s+seats?|wc\s+bowls?|wc\s+pans?|water\s+closets?|lavatory\s+bowls?"
  r"|\btoilets?\b(?!\s*rooms?)|toilets?\s*\(wc\)"
  r"|\bwcs?\b(?!\s*(?:rooms?|tila|huone|ruum|rum|ruimte|r[aä]um))|\bwc's\b"
  r"|\bwc\b(?!\s*(?:rooms?|tila|huone|ruum|rum|ruimte|r[aä]um|séparé|separat|indipendent|independient|osobn|wydzielon))"
  r"|wc-istui\w*|wc-pönt\w*|wc-schüssel\w*|wc-becken\w*|toilettenbecken\w*|klosett\w*|toiletten?(?!\s*r[aä]um)"
  r"|cuvettes?|sièges?\s+de\s+wc|toilettes?(?!\s*(?:séparé|indépendant))"
  r"|\bvasi\b|tazz[ae]\s+wc|sanitari\s+di\s+tipo\s+wc|gabinett[oi]"
  r"|wc-stol\w*|toalettstol\w*|toaletter?(?!\s*(?:rum|utrymme))"
  r"|wc-pott\w*|tualettpott\w*"
  r"|toiletpot\w*|wc-pot\w*|closetpot\w*|toiletten(?!\s*ruimte)"
  r"|inodoros?|retretes?|tazas?\s+de\s+(?:wc|inodoro)"
  r"|misk\w*\s+ustępow\w*|muszl\w*\s+(?:wc|klozet\w*|ustępow\w*)|sedes\w*|urządze\w*\s+wc|\btoalet(?:[ayę]|ach|om)?\b"),

 ("washbasin",
  r"washbasins?|wash\s+basins?|hand\s*basins?|lavabos?|vasques?|éviers?|lavabi|\blavabo\b|lavandin[oi]|lavell[oi]"
  r"|waschbecken\w*|waschtisch\w*|spülbecken\w*|handwaschbecken\w*|\bsinks?\b"
  r"|(?:käsien)?pesuall\w*|tiskiall\w*|(?:käsien)?pesualta\w*|tiskialta\w*|(?<!uima-)\ballas\b|\ballasta\b|\baltaita\b"
  r"|tvättställ\w*|handfat\w*|diskho\w*|tvättfat\w*"
  r"|valamu\w*|kraanikaus\w*"
  r"|håndvask\w*|wastafel\w*|spoelbak\w*|gootste\w*|fonteintje\w*"
  r"|lavamanos|fregaderos?|piletas?"
  r"|umywal\w*|zlewozmywak\w*|\bzlew(?:y|ów|u)?\b"),

 ("bathtub",
  r"bathtubs?|\btubs?\b|bath\s+tubs?|badewann\w*|baignoires?|vasch[ea]\s+da\s+bagno|(?<!due\s)(?<!doppia\s)\bvasch?[ea]\b"
  r"|kylpyamme\w*|\bamme(?:tta|ita|iden|et)?\b|badkar\w*|\bvann(?:id|i|e|ide)?\b|badekar\w*"
  r"|ligbad\w*|badkuip\w*|bañeras?|tinas?|\bwann(?:[ayęo]|ach)?\b|\bwanien\b"),

 ("shower",
  r"showers?|dusch(?:e|en|ar|arna)?\b|douches?\b|(?:box\s+(?:o\s+)?|piatt[oi]\s+)?docc[ie]a?|suihku\w*|dušš\w*|duši\w*|\bdušid\b|bruser\w*"
  r"|duchas?|platos?\s+de\s+ducha|prysznic\w*|natrysk\w*"),

 ("door",
  r"\bdoors?\b|\bdoorways?\b|deuren|\bdeur\b|binnendeur\w*|toegangsdeur\w*|buitendeur\w*"
  r"|puertas?|\buși\b|\busi\b|\bdrzwi\w*\b|türen|\btür\b|innentür\w*|eingangstür\w*|außentür\w*|aussentür\w*"
  r"|\bportes?\b|\bporta\b|portoni|\bov(?:i|ea|ia|et|ien)\b|sisäov\w*|ulko-ov\w*|väliov\w*|sisäänkäyntiov\w*"
  r"|\bdörr(?:ar|arna|en)?\b|innerdörr\w*|entrédörr\w*|ytterdörr\w*"
  r"|\buks(?:ed|i|e)?\b|\bust(?:e)?\b|siseuks\w*|välisuks\w*|\bdøre\b|\bdør\b"),

 ("window",
  r"\bwindows?\b|\bramen\b|\braam\b|vensters?|ventanas?|finestr[ae]|ferestre|\bokien\b|\bokn[ao]\b|\boknami\b"
  r"|fenster\w*|fenêtres?|ikkun\w*|\bfönster\b|\bfönstren\b|\baken\w*\b|\bakna\w*\b|\bvinduer?\b"),

 ("stair",
  r"\bstairs?\b|staircases?|stairways?|\btrappen\b|\btrap\b|escaleras?(?!\s+mecánic)|\bscale\b|\bscala\b(?!\s+mobile)"
  r"|scări|scari|\bschod(?:y|ów|om|ach)\b(?!\s+ruchom)|treppen?(?!haus)|escaliers?(?!\s+mécanique)"
  r"|portaat|portaiden|portaikko\w*|porras\w*|\btrapp(?:a|or|orna|an)?\b|\btrepp(?:id|i|e|ide)?\b|\btrepid\b|\btrepi\b|\btrapper?\b"),

 ("column",
  r"\bcolumns?\b|\bcols?\b|\bpillars?\b|kolommen|\bkolom\b|pilaren|pilares|\bpilar\b|pilastri|\bpilastro\b|\bcolonne\b"
  r"|stâlpi|stalpi|\bsłup\w*\b|\bkolumn\w*\b|columnas?|stützen|\bstütze\b|säulen|\bpoteaux?\b"
  r"|\bpilar(?:i|ia|ien|eita|it)\b|pylvä\w*|\bpelare\b|\bpelarna\b|\bpost(?:id|ide|e|i)?\b|\bsambad\b|\bsammast\b|\bsøjler?\b"),

 ("beam",
  r"\bbeams?\b|\bgirders?\b|\bbalken\b|\bliggers\b|\bligger\b(?!\s+(?:på|i\b|vid|inom))|\bvigas?\b|grinzi|\bbelek\b|\bbelki\b|\bbelk\w*\b|\bdźwigar\w*\b"
  r"|\btravi\b|\btrave\b|\bträger\w*\b|\bpoutres?\b|\bpalk(?:ki|keja|kia|it|kien)\b|\bbalk(?:ar|arna)?\b|\btala(?:d|de|sid)?\b|\bbjælker?\b"),

 ("slab",
  r"\bslabs?\b|vloerplaten|vloerplaat|\bvloeren\b|\bvloer\b|losas?|forjados?|\bsolai\b|\bsolaio\b|solette"
  r"|plăci|placi|\bpłyt\w*\b|\bstrop\w*\b|\bdecken\b|bodenplatten?|deckenplatten?|geschossdecken?|\bplatten\b"
  r"|\bdalles?\b|\blaatta\b|\blaattoj\w*\b|\blaatat\b|\blaatan\b|välipohj\w*"
  r"|\bbjälklag\w*\b|bjälklagsplatt\w*|\bplaat(?:ide|e|id)?\b|vahelag\w*|\bdæk\b"),

 ("wall",
  r"\bwalls?\b|wanden|\bmuren\b|\bmuur\b|\bmuros?\b|\bparedes?\b|\bpared\b|\bmuri\b|\bpareti\b|\bparete\b|muratur[ae]|setti\s+murari"
  r"|pereți|pereti|\bścian\w*\b|\bscian\w*\b|\bwände\w*\b|\bwand\b|\bmurs?\b|\bparois?\b|\bvoiles?\b"
  r"|\bsein(?:ä|iä|ien|ät|än|ää)\b|\bvägg(?:ar|arna|en)?\b|\bsein(?:ad|te|u|a)?\b|\bvægge?\b|\bvæg\b"),

 ("room",
  r"(?<!parking )(?<!car )\bspaces?\b|\brooms?\b|\brms?\b|\bruimtes?\b|\bruimten\b|binnenruimte\w*|\bkamers?\b|\bvertrek\w*\b"
  r"|\blocal(?:es)?\b|\bespacios?\b|\bestancias?\b|\bhabitaciones?\b|\bhabitación\b|\brecintos?\b"
  r"|\blocal[ei]\b|\bstanz[ae]\b|\bvani\b|\bvano\b(?!\s+scala)|\bambient[ei]\b"
  r"|încăperi|incaperi|\bcamere\b|\bpomieszcze\w*\b|\bpoko(?:je|i|jów|ju)\b|\blokali\b"
  r"|\bräume\w*\b|innenr[aä]um\w*|\braum(?:es|s)?\b|\bzimmer\w*\b|\bpièces?\b|\blocaux\b"
  r"|\bhuone(?:et|ita|iden|tta|en|essa|eseen|ella)?\b|sisähuon\w*|sisätil\w*|\btil(?:at|oja|ojen|aa|an)\b"
  r"|\bruum(?:id|e|ide|i)?\b|siseruum\w*|\btubad\w*\b|\brum(?:men|met|s)?\b|inomhusrum\w*|\butrymme\w*\b"),

 ("sanitary",
  r"sanitaire\s+(?:toestellen|voorzieningen)|stuks?\s+sanitair|\bsanitair\b"
  r"|aparatos?\s+sanitarios?|piezas?\s+sanitarias?|equipamientos?\s+sanitarios?"
  r"|apparecchi\s+sanitari|dotazioni\s+sanitarie|\bsanitari\b|obiecte\s+sanitare"
  r"|(?:urządze\w*|przybor\w*|element\w*)\s+sanitarn\w*"
  r"|sanitary(?:\s+fixtures?|\s+appliances?|\s+elements?)?|plumbing\s+fixtures?"
  r"|sanitär\w*|appareils?\s+sanitaires?|équipements?\s+sanitaires?"
  r"|vesikalust\w*|lvi[- ]?kalust\w*|saniteetti\w*|sanitets\w*|sanitaartehni\w*|\bsanitaarsead\w*"),

 ("light",
  r"corpuri\s+de\s+iluminat|verlichtings\w*|lichtarmatur\w*|\barmaturen\b"
  r"|luminarias?|puntos?\s+de\s+luz|apparecchi\s+di\s+illuminazione|corpi\s+illuminanti|punti\s+luce"
  r"|opraw\w*(?:\s+oświetleniow\w*)?|punkt\w*\s+świetln\w*"
  r"|light(?:ing)?\s+fixtures?|luminaires?|appareils?\s+d'éclairage|\blamps?\b"
  r"|\bleuchten\b|beleuchtungskörper\w*|valaisi\w*|belysnings\w*|ljusarmatur\w*|valgust\w*|\blamper\b"),

 ("airterminal", r"air\s+terminals?|diffusers?|luftauslass|luftauslässe|bouches?\s+d'air"),
]

# --- room types (canonical rtype, regex over a ROOM LABEL — DO NOT alter existing label mapping) -------------
RTYPES = [
 ("outdoor",  r"balkon|balcon|balcone|loggia|terrass|terrazz|veranda|parvek|terassi|rõdu|altan|outdoor"),
 ("bathroom", r"^bad$|badezimmer|^bain|salle de bain|bagno|dusche|douche|^kh$|^kph|^ph$|pesuh|kylpyh|^sh$|^psh|bath|vannituba|badeværelse"),
 ("wc",       r"^wc|toilet|toilette|gäste|^wc-|vessa|tualett|^inva-wc"),
 ("kitchen",  r"küche|kueche|cuisine|cucina|kitchen|keitti|^k$|^kt$|^kk$|^kkh$|köök|køkken|kochen"),
 ("bedroom",  r"schlafzimmer|chambre|camera da letto|bedroom|makuuh|^mh\d?$|magamis|soverum"),
 ("living",   r"wohnen|séjour|sejour|soggiorno|living|olohuone|^oh$|elutuba|stue|pranzo|repas|essen|dining"),
 ("corridor", r"korridor|couloir|corridoio|flur|corridor|hall|eteinen|^et$|esik|koridor|gang|entrée|ingresso|eingang"),
 ("storage",  r"reduit|réduit|ripostiglio|abstell|storage|varasto|^var|panipaik|depot|keller|cave|cantina"),
 ("stair",    r"treppe|escalier|scala|stair|porras|trepp|trappe"),
 ("sauna",    r"^s$|sauna|löyly"),
 ("elevator", r"lift|ascenseur|ascensore|elevator|hissi"),
 ("shaft",    r"schacht|gaine|cavedio|shaft|skakt|void|luftraum|šaht"),
 ("office",   r"büro|bureau|ufficio|office|toimisto|kontor"),
 ("room",     r"^zimmer$|^pièce$|^stanza$|^room$|^huone$"),
]

# words in a QUESTION that ask for a room type (used when the question names a room type, e.g. "bathroom area")
RTYPE_IN_Q = [
 ("bathroom",
  r"bathrooms?|washrooms?|shower\s+rooms?|badezimmer\w*|waschr[aä]um\w*|duschr[aä]um\w*|\bbäder\w*\b|\bbad\b"
  r"|salles?\s+de\s+(?:bains?|lavage|douche)|salles?\s+d'eau|\bbagn[oi]\b|sale\s+da\s+bagno|local[ei]\s+doccia"
  r"|kylpyhuon\w*|pesuhuon\w*|kylpy-\s*ja\s*pesuhuon\w*"
  r"|badrum\w*|tvättrum\w*|duschrum\w*|bad-\s*och\s*tvättrum\w*"
  r"|vannitub\w*|vannitoa\w*|pesuruum\w*|badeværels\w*"
  r"|badkamer\w*|wasruimte\w*|doucheruimte\w*"
  r"|cuartos?\s+de\s+baño|salas?\s+de\s+(?:baño|aseo|lavado)|baños?(?!\s+inodoro)"
  r"|łazien\w*|umywalni\w*|prysznicowni\w*|pomieszcze\w*\s+kąpielow\w*"),

 ("wc",
  r"toilet\s+rooms?|wc[- ]rooms?|separate\s+wcs?|lavatory\s+rooms?"
  r"|wc-tila\w*|wc-huon\w*|vessa\w*|wc-r[aä]um\w*|toilettenr[aä]um\w*|gäste-wc\w*"
  r"|locaux\s+wc|pièces?\s+(?:de\s+)?(?:wc|toilettes?)|cabinets?\s+de\s+toilettes?"
  r"|local[ei]\s+(?:wc|toilette)|vani\s+wc|stanze\s+[a-zà-ú ]*\bwc\b|servizi\s+igienici"
  r"|wc-rum\w*|toalettrum\w*|toalettutrymme\w*"
  r"|wc-ruum\w*|tualettruum\w*|tualett\w*"
  r"|wc-ruimte\w*|toiletruimte\w*"
  r"|aseos?\s+(?:independientes?|separados?)|cuartos?\s+de\s+(?:aseo|wc)|recintos?\s+de\s+wc|locales?\s+de\s+wc|estancias?\s+de\s+inodoro"
  r"|pomieszcze\w*\s+(?:wc|ustępow\w*)|(?:osobn\w*|wydzielon\w*)\s+toalet\w*"
  r"|toilets?|\bwcs?\b|\bwc\b|toilette"),

 ("kitchen",
  r"kitchenettes?|kitchens?|küchen?|kochnisch\w*|cuisines?|cucin[ae]|angol[oi]\s+cottura"
  r"|keittiö\w*|keittokomero\w*|apukeittiö\w*|avokeittiö\w*|\bkök(?:et|en|s)?\b|kokvrå\w*|pentry\w*"
  r"|\bköök\w*\b|\bköögi\w*\b|kööginur\w*|\bkøkken\w*\b"
  r"|keuken\w*|bijkeuken\w*|cocinas?|kuchni\w*|kuchen\w*|aneks\w*\s+kuchenn\w*"),

 ("bedroom",
  r"bedrooms?|schlafzimmer\w*|chambres?(?:\s+à\s+coucher)?|camer[ae]\s+da\s+letto|stanze\s+da\s+letto"
  r"|makuuhuon\w*|sovrum\w*|magamistub\w*|magamistoa\w*|soveværels\w*"
  r"|slaapkamer\w*|dormitorios?|habitaciones?\s+dormitorio|sypialni\w*|\bsypialń\b"),

 ("living",
  r"living\s+rooms?|wohnzimmer\w*|\bwohnen\b|séjours?|salons?|soggiorn[oi]|olohuon\w*"
  r"|vardagsrum\w*|elutub\w*|elutoa\w*|\bstue\b|woonkamer\w*|salón|salones|sala\s+de\s+estar|poko\w*\s+dzienn\w*"),

 ("corridor",
  r"corridors?|hallways?|korridor\w*|\bflure?\b|couloirs?|corridoi\w*|eteis\w*|eteinen|koridor\w*|pasillos?|korytarz\w*|przedpoko\w*"),

 ("storage",
  r"storage(?:\s+rooms?)?|reduit\w*|réduit\w*|ripostigli\w*|abstellr[aä]um\w*|varasto\w*"
  r"|förråd\w*|panipaik\w*|panipaig\w*|berging\w*|trasteros?|schow\w*|komór\w*|pomieszcze\w*\s+gospodarcz\w*"),

 ("outdoor",
  r"balcon(?:ies|y)|balkong\w*|balkon\w*|balcones?|balcons?|balcon[ei]|terrass\w*|terrazz\w*|terraces?|terrazas?|terass\w*"
  r"|loggias?|loggie|loggior\w*|parvek\w*|rõdu\w*|altan\w*|buitenruimte\w*|taras\w*"),

 ("stair",
  r"stairwells?|treppenh[aä]us\w*|cages?\s+d'escalier|van[oi]\s+scala|porraskäytäv\w*|trapphus\w*|trepikod\w*|trepikoja\w*|trappenhui\w*|klat\w*\s+schodow\w*"),

 ("sauna",
  r"saunas?|sauna\w*|saunen|saune\b|saunoj\w*|\bbastu\w*\b|\bsaun(?:a|ad|u|ade)?\b|\blöylyhuon\w*"),
]

# --- aggregates & question intents ---------------------------------------------------------------------------
EXISTS = (
 r"^\s*(?:is\s+there|are\s+there|does\s+(?:this|the)|do\s+(?:any|the)|has\s+(?:this|the)|have\s+any"
 r"|onko\b|löytyykö\b|sisältääkö\b|gibt\s+es|existier\w*|hat\s+(?:dies\w*|das|der|die)|enthält|befind\w*\s+sich"
 r"|y\s+a-t-il|est-ce\s+qu'il\s+y\s+a|existe-t-il|c'è\b|ci\s+sono\b|esist(?:e|ono)\b"
 r"|\bkas\b|er\s+der\b|finns\s+det\b|har\s+(?:den|det|denna|detta|planritningen)|innehåller"
 r"|is\s+er\b|zijn\s+er\b|bevat\s+(?:deze|dit|het)|heeft\s+(?:deze|dit|het)"
 r"|¿?\s*hay\b|¿?\s*existe(?:n)?\b|¿?\s*(?:tiene|contiene|dispone)\b|\bczy\b)"
)

COUNT = (
 r"\bcount\b|\bqty\b|\bquantity\s+of\b|how\s+many|number\s+of|#(?=\s*[a-z])"
 r"|wie\s+viele|wieviele|\w*anzahl\b|\bgesamtzahl\b|\bstückzahl\w*\b|\bzähl\w*\b|\bnachzähl\w*\b"
 r"|\bcombien\b|\bnombre\b|\bdécompte\b|\bdénombr\w*\b|\bcomptez\b|\bcompter\b|\bquantité\b"
 r"|\bquant[ie]\b|\bnumero\b|\bconteggi\w*\b|\bconta(?:re)?\b|\bquantità\b|totale\s+(?:degli|delle|dei)\b"
 r"|(?:kuinka|miten)\s+(?:monta|monet)|\bmontako\b|\bmonetko\b|\blukumäär\w*\b|\bkokonaismäär\w*\b|\byhteismäär\w*\b|\bmäärä\b|\blaske\b"
 r"|hur\s+många|\bantal(?:et)?\b|\bräkna\b"
 r"|\bmitu\b|kui\s+palju|\barv\b|\bkoguarv\w*\b|\bloenda\w*\b|\bloe\b"
 r"|\bhoeveel\b|\baantal\b|\btotaalaantal\b|\btel\b|\btellen\b"
 r"|¿?\s*cuánt[oa]s\b|¿?\s*cuant[oa]s\b|\bnúmero\b|\bcantidad\b|\brecuento\b|\bcontabiliz\w*\b|\bcontar\b|\bcuente\b|\bcuenta\b"
 r"|qué\s+total\s+de|\btotal\s+de\s+(?:puertas|ventanas|pilares|columnas|vigas|escaleras|muros|paredes|espacios|habitaciones|locales|estancias|recintos|inodoros|lavabos|bañeras|duchas|pisos|plantas|apartamentos|viviendas|luminarias|aparatos|piezas)"
 r"|\bile\b(?!\s+wynosi|\s+ma\s+(?:powierzchni|długości|objętości))|\bliczb[aęy]\b|\bilość\b|\bpolicz\w*\b|\bzlicz\w*\b"
 r"|hvor\s+mange|\btæl\b|numărul\s+de|numarul\s+de|\bcâte\b|\bcâți\b"
)

SIDE_AREA = (
 r"(?:side|lateral)\s+(?:surface\s+)?area|seitenfläche\w*|wandansichtsfläche\w*|wandseitenfläche\w*|mantelfläche\w*"
 r"|(?:surface|superficie|aire)(?:\s+nette)?\s+latérale|(?:superficie|area)(?:\s+netta)?\s+laterale"
 r"|(?:netto|kokonais|kokonaisnetto)?sivupinta-?al\w*|(?:\b|netto)sidoarea\w*|(?:\b|netto)sidoyta\w*"
 r"|külgpind\w*|zijoppervlak\w*|zijvlak\w*|wandoppervlak\w*.*zijkant|(?:superficie|área|area)(?:\s+neta)?\s+lateral"
 r"|powierzchni\w*\s+boczn\w*|sideareal\w*|nettosideareal\w*|suprafa\w*\s+lateral\w*"
)

AREA = (
 r"oppervlak\w*|vloeroppervlak\w*|superficie|metratura|calpestabil\w*|\baree\b|powierzchni\w*|metraż\w*|suprafa\w*"
 r"|\bareas?\b|\bárea\b|\báreas\b|fläche\w*|grundfläche\w*|wohnfläche\w*|nutzfläche\w*|surface\w*|\baire\b"
 r"|pinta-?al\w*|nettoal\w*|huoneistoal\w*|lattiapinta\w*|pindal\w*|netopind\w*|põrandapind\w*|kogunetopind\w*"
 r"|\bareal\w*\b|\byta\b|\bytan\b|golvarea\w*|golvyta\w*|boarea\w*|nettogolvarea\w*"
 r"|m²|\bm2\b|\bsqm\b|square\s+met\w*|sq\.?\s*m\b|square\s+f(?:ee|oo)t|sq\.?\s*ft\b|ft²|\bft2\b|cm²|\bcm2\b|square\s+centimet\w*"
 r"|quadratmet\w*|quadratfuss|quadratfuß|mètres?\s+carrés?|pieds?\s+carrés?|metri\s+quadrati|neliömetr\w*"
 r"|kvadratmet\w*|ruutmeet\w*|vierkante\s+meter\w*|metros?\s+cuadrados?|pies?\s+cuadrados?|metr\w*\s+kwadratow\w*|stop\w*\s+kwadratow\w*"
 r"|\bsize\b|\bgrö(?:ß|ss)e\b|wie\s+gro(?:ß|ss)|grandezza|ampiezza|\btaille\b|\bkoko\b|kuinka\s+suuri|\bsuurus\b|kui\s+suur|størrelse"
 r"|hur\s+stor\w*|hoe\s+groot|omvang|tamaño|qué\s+tamaño|how\s+big|how\s+large|jak\s+duż\w*"
)

LENGTH = (
 r"\blengte\b|totale\s+lengte|\blongitud\b|\blargo\b|długoś\w*|dlugosc\w*|lungim\w*"
 r"|\blengths?\b|\blen\b|\blänge\w*\b|wie\s+lang\b|\blongueur\w*\b|\blunghezz\w*\b|sviluppo\s+lineare"
 r"|\bpituu\w*\b|\bpikkus\w*\b|\bkogupikkus\w*\b|\blängd\w*\b|\blængde\w*\b"
 r"|perimeter|umfang|périmètre|perimetro|omkrets|omtrek|obwód"
 r"|\bin\s+(?:metres?|meters?|cm|mm|centimet\w*|millimet\w*|zentimet\w*|centimètr\w*|millimètr\w*|centimetr\w*|millimetr\w*|centímetr\w*|milímetr\w*|feet|ft)\b"
 r"|\b(?:linear\s+)?(?:metres?|meters?|metern?|mètres?|metri|metriä|metreinä|meter|metros|metrach)\b"
)

VOLUME = (
 r"objętoś\w*|objetosc\w*|kubatur\w*|cubatura|volumul|\bvolumes?\b|\bvol\b|\bvolumen\w*\b|\binhoud\b"
 r"|tilavuu\w*|nettotilavuu\w*|kokonaisnettotilavuu\w*|\bmaht\w*\b|netomaht\w*|kogunetomaht\w*"
 r"|rumfang|\bvolym\w*\b|nettovolym\w*|m³|\bm3\b|cubic\s+met\w*|kubikmet\w*"
 r"|mètres?\s+cubes?|metri\s+cubi|kuutiometr\w*|kuupmeet\w*|kubieke\s+meter\w*|metros?\s+cúbicos?|metr\w*\s+sześcienn\w*"
)

LARGEST = (
 r"\blargest\b|\bbiggest\b|grö(?:ß|ss)t\w*|plus\s+grand\w*|più\s+grand\w*|\bmaggior\w*\b"
 r"|\bsuurin\b|\bsuurim(?:man|mat|pia|malta)?\b|\bsuurim(?:a|ad)?\b|\bstörst\w*\b|\bstørst\w*\b"
 r"|\bgrootst\w*\b|más\s+grand\w*|\bmayor\b|największ\w*|\bmaximum\b|\bmax\b"
)

SMALLEST = (
 r"\bsmallest\b|\btiniest\b|\bkleinst\w*\b|plus\s+petit\w*|più\s+piccol\w*|\bminor\w*\b"
 r"|\bpienin\b|\bpienimm\w*\b|\bväikseim\w*\b|\bminst\w*\b|\bmindst\w*\b|\bkleinst\w*\b"
 r"|más\s+pequeñ\w*|\bmenor\b|najmniejsz\w*|\bminimum\b|\bmin\b"
)

AVERAGE = (
 r"\baverage\b|\bmean\b|durchschnitt\w*|\bmittelwert\b|\bmoyenn?\w*\b|\bmedi[ao]\b"
 r"|keskimäär\w*|keskiarvo\w*|genomsnitt\w*|medel(?:area|yta|värde)?\w*|keskmi(?:ne|se|st)\b"
 r"|gennemsnit\w*|gemiddeld\w*|promedi\w*|średni\w*"
)

TOTAL = (
 r"totale?|łączn\w*|całkowit\w*|sumaryczn\w*|totală|in\s+total|altogether|gesamt\w*|insgesamt|sämtlich\w*"
 r"|yhteensä|kaikkiaan|yhteenlasket\w*|kokku|kogu\b|summaarn\w*|samlede|sammanlagd\w*|totalt|samtliga"
 r"|gezamenlijk\w*|totaal\w*|conjunto|acumulad\w*|cumulé\w*|complessiv\w*|combined|sum\s+of|all\s+rooms|alle"
)

INDOOR_TOTAL = (
 r"all\s+numbered\s+indoor\s+rooms|toutes\s+les\s+pièces\s+intérieures|aller\s+nummerierten\s+innenräume"
 r"|tutti\s+i\s+locali\s+interni|kaikkien\s+numeroitujen\s+sisätil|alla\s+numrerade\s+inomhus"
 r"|kõigi\s+nummerdatud\s+siseruum|alle\s+genummerde\s+binnenruimte|todos\s+los\s+(?:locales|espacios)\s+interiores\s+numerados"
 r"|wszystkich\s+ponumerowanych\s+pomieszczeń\s+wewnętrznych"
 r"|\bindoor\b|innenr[aä]um\w*|pièces?\s+intérieures?|locaux\s+intérieurs?|(?:locali|vani|ambienti)\s+interni"
 r"|sisätil\w*|sisähuon\w*|inomhus\w*|siseruum\w*|binnenruimte\w*|(?:locales|espacios|estancias|habitaciones)\s+interiores|pomieszcze\w*\s+wewnętrzn\w*"
)

OUTDOOR_WORDS = (
 r"\bhors\b|except\b|excluding\b|excl\.|without\b|apart\s+from|not\s+counting|not\s+including|leaving\s+out"
 r"|sans\b|hormis\b|sauf\b|à\s+l'exception|excluant"
 r"|ohne\b|ausser\b|außer\b|ausgenommen\b|exklusive\b|exkl\.|abzüglich\b|auszuschlie(?:ß|ss)en"
 r"|eccetto\b|ad\s+eccezione|esclus[eioa]\b|escludendo\b|senza\b"
 r"|lukuun\s+ottamatta|ilman\b|\bpaitsi\b|pois\s*lukien|ei\s+lasketa|pois\s+laskuista"
 r"|välja\s+arvatud|v\.a\.|ilma\b|arvestamata"
 r"|undtagen\b|uden\b|exklusive\b|utan\b|utom\b|bortsett\s+från|borträknat|inte\s+räknar"
 r"|zonder\b|uitgezonderd\b|exclusief\b|met\s+uitzondering\s+van|niet\s+meegerekend"
 r"|\bsin\b|excepto\b|excluyendo\b|excluid[oa]s?\b|a\s+excepción\s+de"
 r"|\bbez\b|z\s+wyłączeniem|nie\s+licząc|z\s+pominięciem|pomijając|\bpoza\b"
)

# General multilingual blueprint-QA conversational / framing / visibility / locative stems (recognised as known vocabulary)
PHRASING_RX = (
 # English
 r"\b(?:give|tell|provide|calculate|compute|determine|find|state|report|indicate|specify|show|shows|shown|showing|draw|drawn|drawing|drawings"
 r"|depict\w*|display\w*|illustrat\w*|mark\w*|label\w*|nam\w*|designat\w*|call\w*|model\w*|locat\w*|situat\w*|present\w*|visibl\w*|appear\w*"
 r"|contain\w*|includ\w*|combin\w*|summ\w*|add\w*|count\w*|measur\w*|layout\w*|blueprint\w*|floorplan\w*|diagram\w*|plan\w*|sheet\w*"
 r"|storey\w*|story|stories|level\w*|lvl|building\w*|project\w*|entire|whole|overall|exact\w*|actual\w*|individual\w*|separat\w*|singl\w*"
 r"|togeth\w*|altogeth\w*|across|within|throughout|here|please|kindly|could|would|can|you|your|know|like|want|need|looking|fitted|open"
 r"|interior|internal|entrance|front|main|numbered|indoor|outdoor|external|exterior|glazed|utility|double|bowl|seat|seats|leaf|leaves"
 # German
 r"|zähl\w*|geb\w*|angeb\w*|berechn\w*|ermittel\w*|bestimm\w*|nenn\w*|sag\w*|zeig\w*|abgebildet\w*|dargestellt\w*|eingezeichnet\w*"
 r"|verzeichnet\w*|gekennzeichnet\w*|beschriftet\w*|markiert\w*|nummerier\w*|enthalten\w*|vorhanden\w*|befind\w*|verfüg\w*|sichtbar\w*|lieb\w*|bitte"
 r"|könn\w*|müss\w*|würd\w*|möcht\w*|wüss\w*|wiss\w*|gern\w*|dies\w*|jed\w*|welch\w*|sämtlich\w*|eigenständig\w*|separat\w*|einzeln\w*"
 r"|insgesamt|zusammen\w*|sowohl|als|auch|beziehungsweise|bzw|oder|plus|inklusive|ausschließlich|nur|genau\w*|exakt\w*|gesamt\w*|netto\w*"
 r"|grundriss\w*|plan\w*|zeichnung\w*|ebene\w*|geschoss\w*|stockwerk\w*|etage\w*|modell\w*|gebäude\w*|durchg[aä]ng\w*|eingebaut\w*|türöffnung\w*"
 # French
 r"|compt\w*|donn\w*|indiqu\w*|calcul\w*|précis\w*|détermin\w*|trouv\w*|figur\w*|apparaiss\w*|apparaît|représent\w*|dessin\w*|trac\w*"
 r"|affich\w*|montr\w*|visibl\w*|présent\w*|situ\w*|marqu\w*|repér\w*|not\w*|désign\w*|nomm\w*|étiquet\w*|numérot\w*|portant|intitul\w*|inclus\w*|compris\w*"
 r"|cumul\w*|global\w*|exact\w*|entier\w*|entièr\w*|ensembl\w*|tous|toutes|tout|chaque|quel\w*|combien|pourr\w*|pouv\w*|voudr\w*|aimer\w*"
 r"|savoir|connaîtr\w*|veuill\w*|cet|cette|ces|dans|sur|sous|pour|avec|entre|ainsi|que|qui|dont|soit|uniquement|seulement"
 r"|maquette\w*|bâtiment\w*|niveau\w*|étage\w*|plancher\w*|sol\b|mural\w*|autonom\w*|secondair\w*|intérieur\w*|extérieur\w*|indépendant\w*|sépar\w*|vitr\w*"
 # Italian
 r"|calcol\w*|cont\w*|indic\w*|fornisc\w*|fornir\w*|specific\w*|determin\w*|sapr\w*|saper\w*|conoscer\w*|vorre\w*|potrest\w*|puo\w*|dir\w*|dimmi"
 r"|present\w*|visibil\w*|mostrat\w*|mostra\w*|disegnat\w*|tracciat\w*|riportat\w*|raffigurat\w*|rappresentat\w*|inserit\w*|previst\w*|inclus\w*"
 r"|situat\w*|ubicat\w*|contrassegnat\w*|denominat\w*|etichettat\w*|chiamat\w*|indicat\w*|adibit\w*|consider\w*|sommand\w*|somma\b"
 r"|complessiv\w*|global\w*|esatt\w*|nett\w*|util\w*|inter\w*|intern\w*|estern\w*|indipendent\w*|separat\w*|singol\w*|unic\w*|sol[oa]\b|vetrat\w*"
 r"|planimetri\w*|piant\w*|disegn\w*|livell\w*|pian\w*|modell\w*|edifici\w*|fabbricat\w*|quest\w*|tutt\w*|ogni|qual\w*|nella|nelle|nello|negli|sulla|sulle|dello|della|delle|degli|dall\w*|all\w*|nell\w*"
 # Finnish
 r"|lask\w*|ilmoit\w*|kerr\w*|määrit\w*|ann\w*|haluais\w*|tietä\w*|voisit\w*|kuinka|monta\w*|mikä|mitkä|paljonko|löyty\w*|näky\w*|esiint\w*"
 r"|esitet\w*|piirret\w*|merkit\w*|varustet\w*|nimet\w*|sisälty\w*|sisältä\w*|kuulu\w*|sijait\w*|olev\w*|kaikk\w*|jokai\w*|kukin|tämä\w*|tässä|tähän|tästä|siinä"
 r"|yhteensä|kaikkiaan|yhteenlasket\w*|kokonais\w*|netto\w*|erilli\w*|varsinai\w*|pelkk\w*|mukaan|sekä|että|tai|eli|kun|jos|niin|kuin|vain"
 r"|pohjapiir\w*|pohjakuv\w*|piirustuk\w*|kerrok\w*|taso\w*|mall\w*|rakennuk\w*|asunn\w*|sisä\w*|ulko\w*|lasitet\w*"
 # Swedish
 r"|räkn\w*|beräkn\w*|ang\w*|bestäm\w*|säg\w*|berätt\w*|vis\w*|inritad\w*|utritad\w*|ritad\w*|markerad\w*|märkt\w*|angivn\w*|beteckn\w*"
 r"|finn\w*|förekomm\w*|innehåll\w*|inkluder\w*|ligg\w*|belägn\w*|synlig\w*|samtlig\w*|all[at]?\b|varje|denna|detta|dessa|här|där"
 r"|kan|skulle|vill|vilja|veta|undrar|hur|vad|vilk\w*|totalt|totala|sammanlagd\w*|nett\w*|exakt\w*|hela|tillsammans|både|och|eller|samt|som|för|från|till|med|inom|på|av"
 r"|planritning\w*|ritning\w*|våning\w*|plan\w*|nivå\w*|modell\w*|byggnad\w*|fristående|separat\w*|inglasad\w*|inomhus\w*|utomhus\w*"
 # Estonian
 r"|loend\w*|loe\b|arvut\w*|ann\w*|andk\w*|lei\w*|ütl\w*|öelg\w*|määra\w*|soov\w*|tahak\w*|tead\w*|palun|kui|palju|mitu|mis|millin\w*|kas"
 r"|kujutat\w*|joonestat\w*|näidat\w*|märgit\w*|tähistat\w*|nimetat\w*|esitat\w*|asuv\w*|paiknev\w*|olemas\w*|sisaldu\w*|sisald\w*|kuulu\w*|võttes|arvesse|kannavad"
 r"|kõik\w*|kõigi\w*|iga\b|see\b|selle\w*|antud|siin|kokku|kogu\w*|summaarn\w*|neto\w*|täpne|täpselt|eraldi\w*|sise\w*|välis\w*|klaasit\w*|avatud"
 r"|korruseplaan\w*|põhiplaan\w*|plaan\w*|joonis\w*|korrus\w*|tasand\w*|tase\w*|mudel\w*|hoon\w*"
 # Dutch
 r"|tel\w*|geef|geven|aangev\w*|bereken\w*|bepaal\w*|bepalen|noem\w*|vertel\w*|nagaan|zien|weten|wil\w*|graag|kun\w*|zou\w*|hoeveel|wat|welk\w*"
 r"|getekend\w*|ingetekend\w*|weergegev\w*|afgebeeld\w*|getoond\w*|aangeduid\w*|gemarkeerd\w*|genoemd\w*|gelabeld\w*|aanwezig\w*|zichtbaar\w*|bevond\w*|bevind\w*|bevat\w*|staan|staat|telt|toont|bedraagt|opgeteld"
 r"|deze|dit|die|dat|elk\w*|ieder\w*|all[e]?\b|totaal\w*|totale|gezamenlijk\w*|volledig\w*|exact\w*|netto\w*|apart\w*|afzonderlijk\w*|samen|inclusief|binnen\w*|buiten\w*|beglaasd\w*"
 r"|plattegrond\w*|bouwtekening\w*|tekening\w*|verdieping\w*|bouwlaag|bouwlagen|niveau\w*|model\w*|gebouw\w*|voor|van|het|een|zijn|er|op|in|bij|met|oftewel"
 # Spanish
 r"|calcul\w*|cuent\w*|cont\w*|indic\w*|indiqu\w*|dime|dig\w*|dec\w*|determin\w*|especific\w*|proporcion\w*|sab\w*|conoc\w*|necesit\w*|gustar\w*|quisier\w*|podr\w*|pued\w*"
 r"|mostrad\w*|muestr\w*|dibujad\w*|representad\w*|figur\w*|aparec\w*|visibl\w*|present\w*|ubicad\w*|situad\w*|incluid\w*|contenid\w*|contien\w*|instalad\w*"
 r"|etiquetad\w*|rotulad\w*|marcad\w*|denominad\w*|llamad\w*|designad\w*|numerad\w*|sumand\w*|conjunt\w*|total\w*|exact\w*|net[oa]s?|útil\w*"
 r"|est[ea]s?|es[ea]s?|tod[oa]s?|cada|qué|cuál\w*|cuánt\w*|cómo|donde|hay|son|están|está|para|por|con|como|sobre|entre|dentro|solo|únicamente"
 r"|plan[oa]s?|planta\w*|nivel\w*|pis[oa]s?|model\w*|edifici\w*|arquitectónic\w*|interior\w*|exterior\w*|independient\w*|separad\w*|acristalad\w*"
 # Polish
 r"|policz\w*|zlicz\w*|oblicz\w*|podaj\w*|wskaż\w*|określ\w*|powiedz\w*|ustal\w*|proszę|chciał\w*|chcę|wiedzie\w*|interesuj\w*|mnie|moż\w*|wiadomo"
 r"|narysowan\w*|wrysowan\w*|zaznaczon\w*|pokazan\w*|przedstawion\w*|widoczn\w*|ukazan\w*|oznaczon\w*|opisan\w*|nazwan\w*|zaprojektowan\w*|uwzględnion\w*|ujęt\w*|zamodelowan\w*"
 r"|znajduj\w*|występuj\w*|obejmuj\w*|zawier\w*|posiad\w*|jest|są|wynosi|stanowi|widać|wszystk\w*|każd\w*|ten|ta|to|tym|tej|tego|te\b|tych"
 r"|łączn\w*|całkowit\w*|sumaryczn\w*|ogóln\w*|dokładn\w*|netto|wewnętrzn\w*|zewnętrzn\w*|osobn\w*|wydzielon\w*|oddzieln\w*|przeszklon\w*|otwart\w*"
 r"|rzut\w*|plan\w*|rysunk\w*|kondygnacj\w*|piętr\w*|poziom\w*|model\w*|budynk\w*|dla|na|w|we|z|ze|do|od|po|przy|oraz|lub|czy|jak\w*|ile)\b"
)

def find(table, text):
    """first (key, match) whose regex matches text (case-insensitive), else (None, None)"""
    for k, rx in table:
        m = re.search(rx, text, re.I)
        if m: return k, m
    return None, None

def find_earliest(table, text):
    best = (None, None)
    for k, rx in table:
        m = re.search(rx, text, re.I)
        if m and (best[1] is None or m.start() < best[1].start()): best = (k, m)
    return best

def rtype_of_label(label):
    if not label: return None
    k, _ = find(RTYPES, label.strip().lower())
    return k
