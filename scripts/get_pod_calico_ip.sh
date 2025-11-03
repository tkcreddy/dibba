for i in $(ip -br addr show | awk '/cali/{print $1}'| awk -F- '{print $1}')
do
pn=$(calicoctl get weps -o wide | grep $i | awk '{print $4}'| sed 's/cali//g')
ip=$(calicoctl get weps -o wide | grep $i | awk '{print $3}')
echo "$pn --> $ip"
done